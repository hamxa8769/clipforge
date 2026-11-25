"""YouTube bot-check / stall-watchdog fix (v0.11.1): the v0.10.3 watchdog
watched stderr only while yt-dlp prints all download progress to
stdout, so any download slower than ~550 KiB/s was killed as
"stalled" while data was flowing — the user's "stalled twice" job
(392 s, video vPKZP1T5m7k). These tests drive
:func:`clipforge.ingest.ingest_youtube` and
:func:`clipforge.ingest._run_ytdlp_bounded` through a fake yt-dlp
whose download runs print real ``[download]`` progress lines to
stdout, stall, or bot-check per mode, so they run without network.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from clipforge.errors import plain_error, sanitize_error_message
from clipforge.ingest import (
    DownloadProgress,
    IngestError,
    _DownloadRung,
    _run_ytdlp_bounded,
    check_youtube_available,
    classify_ytdlp_error,
    has_sabr_warning,
    ingest_youtube,
    parse_ytdlp_progress_line,
)

LIVE_URL = "https://www.youtube.com/watch?v=M7lc1UVf-VE"
LIVE_ID = "M7lc1UVf-VE"

FAKE_YTDLP = '''#!/usr/bin/env python3
"""Fake yt-dlp for v0.11.1 watchdog/ladder tests: modes via FAKE_YTDLP_MODE."""
import json, os, shutil, sys, time

log_path = os.environ["FAKE_YTDLP_LOG"]
fixture = os.environ["FAKE_YTDLP_FIXTURE"]
state_path = os.environ["FAKE_YTDLP_STATE"]
mode = os.environ.get("FAKE_YTDLP_MODE", "ok")
argv = sys.argv[1:]
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")

if argv == ["--version"]:
    if mode == "broken-version":
        sys.stderr.write("ModuleNotFoundError: No module named 'yt_dlp'\\n")
        sys.exit(1)
    print("2026.11.23")
    sys.exit(0)

android = "youtube:player_client=android" in argv
ios = "youtube:player_client=ios" in argv
fmt = argv[argv.index("--format") + 1] if "--format" in argv else ""
video_id = "M7lc1UVf-VE"

def _run_count():
    try:
        return int(open(state_path, encoding="utf-8").read().strip() or "0")
    except OSError:
        return 0

def _bump_count():
    n = _run_count() + 1
    with open(state_path, "w", encoding="utf-8") as fh:
        fh.write(str(n))
    return n

def _progress(pct, total="96.60MiB", speed="1.54MiB/s", eta="00:10"):
    sys.stdout.write(f"[download] {pct:.1f}% of {total} at {speed} ETA {eta}\\n")
    sys.stdout.flush()

def _finish():
    outtmpl = argv[argv.index("-o") + 1]
    out_dir = os.path.dirname(outtmpl)
    os.makedirs(out_dir, exist_ok=True)
    shutil.copyfile(fixture, os.path.join(out_dir, video_id + ".mp4"))
    sys.exit(0)

BOTCHECK = ("ERROR: [youtube] M7lc1UVf-VE: Sign in to confirm you\\u2019re "
            "not a bot. Use --cookies-from-browser or --cookies for the "
            "authentication.\\n")

if "--simulate" in argv:
    if mode == "unreachable-probe" and not android:
        sys.stderr.write("ERROR: [youtube] M7lc1UVf-VE: Unable to download webpage: "
                         "Remote end closed connection without response\\n")
        sys.exit(1)
    info = {"id": video_id, "title": "Fake video", "duration": 120,
            "is_live": False, "_type": "video"}
    print(json.dumps(info))
    sys.exit(0)

# Download run.
run_no = _bump_count()
if mode == "progress-slow":
    for pct in range(0, 101, 10):
        _progress(pct)
        time.sleep(0.3)
    _finish()
if mode == "progress-then-botcheck":
    for pct in (10.0, 20.0, 30.0):
        _progress(pct)
    sys.stderr.write(BOTCHECK)
    sys.stderr.flush()
    time.sleep(30)
    sys.exit(0)
if mode == "progress-stall-once":
    if run_no == 1:
        for pct in (10.0, 30.0, 50.0):
            _progress(pct)
        time.sleep(30)
        sys.exit(0)
    _finish()
if mode == "ladder-web-stall":
    if not android and not ios:
        time.sleep(30)
        sys.exit(0)
    _finish()
if mode == "ladder-web-botcheck-android-stall":
    if not android and not ios:
        sys.stderr.write(BOTCHECK)
        sys.exit(1)
    time.sleep(30)
    sys.exit(0)
if mode == "ladder-sabr":
    if not android and not ios:
        sys.stderr.write(BOTCHECK)
        sys.exit(1)
    if android and fmt != "best":
        sys.stderr.write("WARNING: [youtube] M7lc1UVf-VE: Some android client https formats "
                         "have been skipped as they are missing a URL. YouTube may have enabled "
                         "the SABR-only streaming experiment for the current session.\\n")
        sys.stderr.write("ERROR: [youtube] M7lc1UVf-VE: No video formats found!\\n")
        sys.exit(1)
    _finish()
_finish()
'''


@pytest.fixture()
def fixture_video(tmp_path: Path) -> Path:
    """A tiny real mp4 so ffprobe validation can run."""
    video = tmp_path / "sample.mp4"
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "10",
            str(video),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0 or not video.exists():
        pytest.skip(f"ffmpeg could not generate a test video: {proc.stderr}")
    return video


@pytest.fixture()
def fake_ytdlp(tmp_path: Path, fixture_video: Path, monkeypatch) -> Path:
    script = tmp_path / "fake-ytdlp.py"
    script.write_text(FAKE_YTDLP, encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "argv.log"
    log.write_text("", encoding="utf-8")
    state = tmp_path / "state.txt"
    state.write_text("0", encoding="utf-8")
    monkeypatch.setenv("FAKE_YTDLP_LOG", str(log))
    monkeypatch.setenv("FAKE_YTDLP_FIXTURE", str(fixture_video))
    monkeypatch.setenv("FAKE_YTDLP_STATE", str(state))
    monkeypatch.setattr("clipforge.ingest.resolve_yt_dlp_path", lambda data_dir=None: str(script))
    monkeypatch.setattr("clipforge.ingest._oembed_status", lambda url: "available")
    # The real stall timeout is 180 s; 1 s keeps the tests fast while
    # still exercising the watchdog + ladder paths.
    monkeypatch.setattr("clipforge.ingest.YTDLP_STALL_TIMEOUT_S", 1)
    return script


def _argv_runs(tmp_path: Path) -> list[list[str]]:
    runs = [
        json.loads(line)
        for line in (tmp_path / "argv.log").read_text().splitlines()
        if line.strip()
    ]
    return [run for run in runs if run != ["--version"]]


def _download_runs(tmp_path: Path) -> list[list[str]]:
    return [a for a in _argv_runs(tmp_path) if "--simulate" not in a]


# ------------------------------------------------------- progress parsing
@pytest.mark.parametrize(
    ("line", "downloaded", "total", "speed", "eta", "percent"),
    [
        (
            "[download]  42.3% of  96.60MiB at  1.54MiB/s ETA 00:33",
            42846702,
            101292441,
            1614807.04,
            33.0,
            42.3,
        ),
        (
            "[download]   0.0% of 96.60MiB at 30.68KiB/s ETA 53:45",
            0,
            101292441,
            31416.32,
            3225.0,
            0.0,
        ),
        (
            "[download] 100% of 96.60MiB in 00:01:02 at 1.54MiB/s",
            101292441,
            101292441,
            1614807.04,
            0.0,
            100.0,
        ),
        # No speed / ETA yet (first lines of a download).
        ("[download]   5.0% of  96.60MiB", 5064622, 101292441, 0.0, None, 5.0),
    ],
)
def test_parse_ytdlp_progress_line(line, downloaded, total, speed, eta, percent) -> None:
    parsed = parse_ytdlp_progress_line(line)
    assert parsed is not None
    assert parsed.downloaded_bytes == downloaded
    assert parsed.total_bytes == total
    assert parsed.speed_bps == pytest.approx(speed)
    assert parsed.eta_s == eta
    assert parsed.percent == percent


@pytest.mark.parametrize(
    "line",
    [
        "[download] Destination: vPKZP1T5m7k.f616.mp4",
        '[Merger] Merging formats into "vPKZP1T5m7k.mp4"',
        "[youtube] Extracting URL: https://www.youtube.com/watch?v=vPKZP1T5m7k",
        "ERROR: [youtube] vPKZP1T5m7k: Sign in to confirm you're not a bot",
        "",
    ],
)
def test_parse_ytdlp_progress_line_ignores_non_progress(line: str) -> None:
    assert parse_ytdlp_progress_line(line) is None


# ------------------------------------------------------- the watchdog itself
def test_stdout_progress_does_not_trip_watchdog(
    tmp_path: Path, fake_ytdlp: Path, monkeypatch
) -> None:
    """The root cause, in miniature: a download that prints progress
    to stdout only (real yt-dlp behaviour — stderr stays silent for
    the whole media phase) and takes longer than the stall timeout
    must NOT be killed. v0.10.3 threw stdout away and killed exactly
    this download at 180 s of stderr silence."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "progress-slow")
    seen: list[DownloadProgress] = []
    info = ingest_youtube(
        LIVE_URL,
        own_content=True,
        out_dir=tmp_path / "uploads",
        on_progress=seen.append,
    )
    assert info.source_kind == "youtube"
    assert seen, "on_progress was never called"
    assert seen[-1].percent == 100.0
    assert seen[-1].downloaded_bytes > 0
    assert seen[-1].speed_bps > 0


def test_stdout_chatty_process_does_not_trip_watchdog() -> None:
    """Steady stdout output resets the stall clock — the v0.10.3 test
    printed to stderr, but real yt-dlp is chatty on stdout."""
    cmd = [
        sys.executable,
        "-c",
        "import sys, time\n"
        "[print(i, file=sys.stdout, flush=True) or time.sleep(0.2) for i in range(10)]",
    ]
    code, stderr, stdout = _run_ytdlp_bounded(cmd, None, timeout=60, stall_timeout=1)
    assert code == 0
    assert "9" in stdout


def test_silent_download_still_stalls_and_reports_no_progress() -> None:
    """A subprocess producing no output anywhere is still killed — and
    the message no longer claims 'no data arrived from YouTube' as a
    diagnosis of the user's connection."""
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    started = time.monotonic()
    with pytest.raises(IngestError) as excinfo:
        _run_ytdlp_bounded(cmd, None, timeout=60, stall_timeout=1)
    assert time.monotonic() - started < 10
    assert excinfo.value.code == "download_stalled"
    assert "no data arrived" not in str(excinfo.value).lower()
    assert "check your internet" not in str(excinfo.value).lower()
    progress = getattr(excinfo.value, "download_progress", None)
    assert progress is not None and progress.downloaded_bytes == 0


def test_stall_after_progress_reports_bytes_and_speed(
    tmp_path: Path, fake_ytdlp: Path, monkeypatch
) -> None:
    """A watchdog kill after real progress reports what was actually
    observed — bytes downloaded and speed — and keeps the preserved
    yt-dlp output on the error instead of dropping it."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "progress-then-botcheck")
    # All rungs print progress then bot-check and stall, so the ladder
    # is exhausted and the bot-check (seen on every rung) wins over
    # the stall — but the progress evidence must survive on the error.
    with pytest.raises(IngestError) as excinfo:
        ingest_youtube(LIVE_URL, own_content=True, out_dir=tmp_path / "uploads")
    assert excinfo.value.code == "youtube_bot_check"
    progress = getattr(excinfo.value, "download_progress", None)
    assert progress is not None and progress.downloaded_bytes > 0
    attached = (getattr(excinfo.value, "stderr_text", "") or "") + (
        getattr(excinfo.value, "stdout_text", "") or ""
    )
    assert "not a bot" in attached


def test_bot_check_detected_on_watchdog_killed_run(
    tmp_path: Path, fake_ytdlp: Path, monkeypatch
) -> None:
    """A run that prints progress, then the bot-check line, then goes
    silent is killed by the watchdog — but the error must be
    ``youtube_bot_check`` (classified from the preserved output), not
    ``download_stalled``."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "progress-then-botcheck")
    with pytest.raises(IngestError) as excinfo:
        ingest_youtube(LIVE_URL, own_content=True, out_dir=tmp_path / "uploads")
    assert excinfo.value.code == "youtube_bot_check"
    assert "sign in" in str(excinfo.value).lower()


def test_stall_with_progress_gets_same_command_resume_retry(
    tmp_path: Path, fake_ytdlp: Path, monkeypatch
) -> None:
    """A stall *with* proven byte progress gets one same-command resume
    retry (the v0.11.0 behaviour, kept): the first run downloads to
    50% then stalls, the identical retry finishes the job."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "progress-stall-once")
    info = ingest_youtube(LIVE_URL, own_content=True, out_dir=tmp_path / "uploads")
    assert info.source_kind == "youtube"
    downloads = _download_runs(tmp_path)
    assert len(downloads) == 2
    assert downloads[0] == downloads[1]  # identical command, same dir


# ------------------------------------------------------- the fallback ladder
def test_ladder_escalates_on_stall_without_identical_retry(
    tmp_path: Path, fake_ytdlp: Path, monkeypatch
) -> None:
    """Web stalls with zero progress → the Android rung is tried next,
    and the web command is never repeated identically (the v0.11.0
    bug: the same stalling command ran twice and the Android
    fallback never ran)."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "ladder-web-stall")
    attempts: list[dict] = []
    info = ingest_youtube(
        LIVE_URL,
        own_content=True,
        out_dir=tmp_path / "uploads",
        on_attempt=attempts.append,
    )
    assert info.source_kind == "youtube"
    downloads = _download_runs(tmp_path)
    assert len(downloads) == 2
    assert "youtube:player_client=android" not in downloads[0]
    assert "youtube:player_client=android" in downloads[1]
    assert [a["client"] for a in attempts] == ["web", "android"]
    assert attempts[0]["error_code"] == "download_stalled"
    assert attempts[1]["error_code"] is None


def test_ladder_order_web_android_android_best(
    tmp_path: Path, fake_ytdlp: Path, monkeypatch
) -> None:
    """SABR-only formats on the Android client jump straight to the
    single-file ``best`` rung: web (bot-check) → android full (SABR)
    → android best (succeeds)."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "ladder-sabr")
    attempts: list[dict] = []
    info = ingest_youtube(
        LIVE_URL,
        own_content=True,
        out_dir=tmp_path / "uploads",
        on_attempt=attempts.append,
    )
    assert info.source_kind == "youtube"
    downloads = _download_runs(tmp_path)
    assert len(downloads) == 3
    assert "youtube:player_client=android" not in downloads[0]
    assert "youtube:player_client=android" in downloads[1]
    assert downloads[2][downloads[2].index("--format") + 1] == "best"
    assert [(a["client"], a["format"]) for a in attempts] == [
        ("web", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"),
        ("android", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"),
        ("android", "best"),
    ]


def test_bot_check_on_web_and_stall_on_android_is_bot_check(
    tmp_path: Path, fake_ytdlp: Path, monkeypatch
) -> None:
    """Web exits fast with bot-check, Android stalls: the final error
    is ``youtube_bot_check``, not ``download_stalled`` — a bot-check
    seen on any rung beats a later stall."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "ladder-web-botcheck-android-stall")
    started = time.monotonic()
    with pytest.raises(IngestError) as excinfo:
        ingest_youtube(LIVE_URL, own_content=True, out_dir=tmp_path / "uploads")
    assert time.monotonic() - started < 30
    assert excinfo.value.code == "youtube_bot_check"
    assert "sign in" in str(excinfo.value).lower()
    downloads = _download_runs(tmp_path)
    assert len(downloads) == 5  # every rung tried once, none repeated


def test_all_rungs_stalled_reports_rungs_tried(
    tmp_path: Path, fake_ytdlp: Path, monkeypatch
) -> None:
    """Every rung stalls with zero progress → the final stall error
    names the rungs that were tried instead of claiming the retry
    already happened on one command."""
    monkeypatch.setattr(
        "clipforge.ingest._DOWNLOAD_RUNGS",
        (_DownloadRung("web", "best", None),),
    )
    monkeypatch.setenv("FAKE_YTDLP_MODE", "ladder-web-stall")
    with pytest.raises(IngestError) as excinfo:
        ingest_youtube(LIVE_URL, own_content=True, out_dir=tmp_path / "uploads")
    assert excinfo.value.code == "download_stalled"
    assert "Tried 1 download(s): web/best" in str(excinfo.value)


# ------------------------------------------------------- cookies + probe
def test_cookies_browser_reaches_ytdlp_argv(tmp_path: Path, fake_ytdlp: Path, monkeypatch) -> None:
    """``cookies_from_browser='chrome'`` is passed to yt-dlp on both
    the probe and the download, as ``--cookies-from-browser chrome``
    — and no cookie *value* appears anywhere (only the browser name
    is ever passed or stored)."""
    info = ingest_youtube(
        LIVE_URL,
        own_content=True,
        out_dir=tmp_path / "uploads",
        cookies_from_browser="chrome",
    )
    assert info.source_kind == "youtube"
    runs = _argv_runs(tmp_path)
    assert runs, "yt-dlp was never invoked"
    for run in runs:
        assert "--cookies-from-browser" in run
        assert run[run.index("--cookies-from-browser") + 1] == "chrome"
    assert "chrome" not in str(info.path)


def test_unknown_cookies_browser_is_ignored(tmp_path: Path, fake_ytdlp: Path, monkeypatch) -> None:
    """An unknown browser name never reaches yt-dlp's argv — it is
    normalised to off instead of being passed through."""
    ingest_youtube(
        LIVE_URL,
        own_content=True,
        out_dir=tmp_path / "uploads",
        cookies_from_browser="safari",
    )
    for run in _argv_runs(tmp_path):
        assert "--cookies-from-browser" not in run


def test_broken_ytdlp_fails_as_missing(tmp_path: Path, fake_ytdlp: Path, monkeypatch) -> None:
    """A yt-dlp binary that exists but does not run (``--version``
    fails) is reported as missing — with the Install button — instead
    of dying mid-download."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "broken-version")
    with pytest.raises(IngestError) as excinfo:
        ingest_youtube(LIVE_URL, own_content=True, out_dir=tmp_path / "uploads")
    assert "yt-dlp" in str(excinfo.value).lower()
    assert "install" in str(excinfo.value).lower()


def test_probe_retries_android_on_unreachable(
    tmp_path: Path, fake_ytdlp: Path, monkeypatch
) -> None:
    """A web probe that fails as unreachable (dropped webpage fetch)
    gets the Android retry too — the Android probe succeeds in
    exactly that situation."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "unreachable-probe")
    info = check_youtube_available(LIVE_URL, str(fake_ytdlp))
    assert info["title"] == "Fake video"
    runs = _argv_runs(tmp_path)
    assert len(runs) == 2
    assert "youtube:player_client=android" in runs[1]


# ------------------------------------------------------- classification
def test_classify_cookies_database_error() -> None:
    assert (
        classify_ytdlp_error(
            'ERROR: could not find chromium cookies database in "/home/user/.config/chromium"'
        )
        == "youtube_cookies_missing"
    )


def test_sabr_warning_detection() -> None:
    assert has_sabr_warning(
        "WARNING: [youtube] X: Some android client https formats have been "
        "skipped as they are missing a URL. YouTube may have enabled the "
        "SABR-only streaming experiment for the current session."
    )
    assert not has_sabr_warning("ERROR: [youtube] X: No video formats found!")


def test_sanitize_redacts_cookie_values() -> None:
    assert sanitize_error_message("download failed; cookie: abc123secret") == (
        "download failed; cookie: (hidden)"
    )


def test_plain_error_bot_check_points_at_browser_cookies() -> None:
    mapped = plain_error(IngestError("x", code="youtube_bot_check"), "ingest")
    assert "browser cookies" in mapped["action"]
    assert "different video" not in mapped["action"]


def test_plain_error_cookies_missing() -> None:
    mapped = plain_error(IngestError("x", code="youtube_cookies_missing"), "ingest")
    assert mapped["code"] == "youtube_cookies_missing"
    assert "Settings" in mapped["action"]


def test_plain_error_stall_names_ladder_not_internet() -> None:
    mapped = plain_error(IngestError("x", code="download_stalled"), "ingest")
    assert "Check your internet connection" not in mapped["action"]
    assert "browser cookies" in mapped["action"]
