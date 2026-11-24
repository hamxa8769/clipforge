"""Fail-fast ingest (v0.10.3): a dead link fails in seconds with a
plain-language reason, and a stalled download is killed by a watchdog.

The user's v0.10.2 job e01b83e18de34ada (Comedy link CMzctRZQlo0,
removed from YouTube) ran 3601 s and died with the generic "This step
took too long". These tests drive :func:`clipforge.ingest.ingest_youtube`
and :func:`clipforge.ingest.check_youtube_available` through a fake
yt-dlp binary (modes via ``FAKE_YTDLP_MODE``), so they run without
network or a real YouTube fetch — the live behaviour is verified
separately in the v0.10.3 E2E run (real yt-dlp: dead link 404 via
oEmbed in ~0.5 s, extractor "Video unavailable" in ~2 s).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from clipforge.errors import plain_error
from clipforge.ingest import (
    IngestError,
    _run_ytdlp_bounded,
    check_youtube_available,
    classify_ytdlp_error,
    ingest_youtube,
)

DEAD_URL = "https://youtu.be/CMzctRZQlo0?si=cb2aCsIfgr4I30pV"
DEAD_ID = "CMzctRZQlo0"
LIVE_URL = "https://www.youtube.com/watch?v=M7lc1UVf-VE"

FAKE_YTDLP = '''#!/usr/bin/env python3
"""Fake yt-dlp for fail-fast tests: modes via FAKE_YTDLP_MODE."""
import json, os, shutil, sys

log_path = os.environ["FAKE_YTDLP_LOG"]
fixture = os.environ["FAKE_YTDLP_FIXTURE"]
mode = os.environ.get("FAKE_YTDLP_MODE", "ok")
argv = sys.argv[1:]
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")

video_id = "CMzctRZQlo0"
android = "youtube:player_client=android" in argv

if "--simulate" in argv:
    if mode == "unavailable":
        sys.stderr.write("ERROR: [youtube] CMzctRZQlo0: This video is unavailable\\n")
        sys.exit(1)
    if mode == "unreachable":
        sys.stderr.write("ERROR: [youtube] CMzctRZQlo0: Unable to download webpage: "
                         "Remote end closed connection without response\\n")
        sys.exit(1)
    if mode == "botcheck" and not android:
        sys.stderr.write("ERROR: [youtube] CMzctRZQlo0: Sign in to confirm you\\u2019re "
                         "not a bot. Use --cookies-from-browser or --cookies for the "
                         "authentication.\\n")
        sys.exit(1)
    if mode == "live":
        info = {"id": video_id, "title": "Live stream", "duration": 0,
                "is_live": True, "_type": "video"}
        print(json.dumps(info))
        sys.exit(0)
    if mode == "playlist":
        info = {"id": "PLxxxx", "title": "A playlist", "_type": "playlist"}
        print(json.dumps(info))
        sys.exit(0)
    info = {"id": video_id, "title": "Fake video", "duration": 120,
            "is_live": False, "_type": "video"}
    print(json.dumps(info))
    sys.exit(0)

# Download run.
if mode == "botcheck-download" and not android:
    sys.stderr.write("ERROR: [youtube] CMzctRZQlo0: Sign in to confirm you\\u2019re "
                     "not a bot. Use --cookies-from-browser or --cookies for the "
                     "authentication.\\n")
    sys.exit(1)
outtmpl = argv[argv.index("-o") + 1]
out_dir = os.path.dirname(outtmpl)
os.makedirs(out_dir, exist_ok=True)
shutil.copyfile(fixture, os.path.join(out_dir, video_id + ".mp4"))
sys.exit(0)
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
    """A fake yt-dlp binary; ``resolve_yt_dlp_path`` points at it."""
    script = tmp_path / "fake-ytdlp.py"
    script.write_text(FAKE_YTDLP, encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "argv.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("FAKE_YTDLP_LOG", str(log))
    monkeypatch.setenv("FAKE_YTDLP_FIXTURE", str(fixture_video))
    monkeypatch.setattr("clipforge.ingest.resolve_yt_dlp_path", lambda data_dir=None: str(script))
    # Default: the link exists (oEmbed 200); individual tests override.
    monkeypatch.setattr("clipforge.ingest._oembed_status", lambda url: "available")
    return script


def _argv_runs(log_path: Path) -> list[list[str]]:
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def _log(tmp_path: Path) -> Path:
    return Path(os.environ["FAKE_YTDLP_LOG"])


# ------------------------------------------------------------ dead link
def test_dead_link_fails_fast_via_oembed(tmp_path: Path, fake_ytdlp: Path, monkeypatch) -> None:
    """oEmbed 404 → unavailable in seconds; yt-dlp is never invoked."""
    monkeypatch.setattr("clipforge.ingest._oembed_status", lambda url: "unavailable")
    started = time.monotonic()
    with pytest.raises(IngestError) as excinfo:
        ingest_youtube(DEAD_URL, own_content=True, out_dir=tmp_path / "uploads")
    assert time.monotonic() - started < 5
    assert excinfo.value.code == "youtube_unavailable"
    assert "no longer available" in str(excinfo.value)
    assert "different link" in str(excinfo.value)
    # The pre-flight stopped the job before any download was attempted.
    assert _argv_runs(_log(tmp_path)) == []


def test_dead_link_fails_fast_via_simulate(tmp_path: Path, fake_ytdlp: Path, monkeypatch) -> None:
    """oEmbed inconclusive + extractor "Video unavailable" → same fast
    failure, still with no download attempt (the v0.10.2 job burned an
    hour here; this path takes under a second)."""
    monkeypatch.setattr("clipforge.ingest._oembed_status", lambda url: "unknown")
    monkeypatch.setenv("FAKE_YTDLP_MODE", "unavailable")
    started = time.monotonic()
    with pytest.raises(IngestError) as excinfo:
        ingest_youtube(DEAD_URL, own_content=True, out_dir=tmp_path / "uploads")
    assert time.monotonic() - started < 10
    assert excinfo.value.code == "youtube_unavailable"
    runs = _argv_runs(_log(tmp_path))
    assert len(runs) == 1  # the probe only — no download run
    assert "--simulate" in runs[0] and "--dump-json" in runs[0]


def test_preflight_probe_is_bounded(tmp_path: Path, fake_ytdlp: Path) -> None:
    """The metadata-only probe carries a socket timeout and no download."""
    check_youtube_available(LIVE_URL, str(fake_ytdlp))
    runs = _argv_runs(_log(tmp_path))
    assert len(runs) == 1
    probe = runs[0]
    assert "--simulate" in probe and "--dump-json" in probe
    assert probe[probe.index("--socket-timeout") + 1] == "15"
    assert probe[probe.index("--extractor-retries") + 1] == "1"
    assert "-o" not in probe


def test_bot_check_retries_android_client(tmp_path: Path, fake_ytdlp: Path, monkeypatch) -> None:
    """Web client bot-blocked → one retry on the Android player client,
    which answers; the job proceeds instead of failing."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "botcheck")
    info = check_youtube_available(DEAD_URL, str(fake_ytdlp))
    assert info["title"] == "Fake video"
    runs = _argv_runs(_log(tmp_path))
    assert len(runs) == 2
    assert "youtube:player_client=android" in runs[1]


def test_live_stream_is_rejected(tmp_path: Path, fake_ytdlp: Path, monkeypatch) -> None:
    """A live URL is refused in the pre-flight — a live download would
    never finish, which is another way to burn the whole timeout."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "live")
    with pytest.raises(IngestError) as excinfo:
        check_youtube_available(LIVE_URL, str(fake_ytdlp))
    assert excinfo.value.code == "youtube_live"
    assert "live stream" in str(excinfo.value).lower()


def test_playlist_is_rejected(tmp_path: Path, fake_ytdlp: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_YTDLP_MODE", "playlist")
    with pytest.raises(IngestError) as excinfo:
        check_youtube_available(LIVE_URL, str(fake_ytdlp))
    assert excinfo.value.code == "youtube_playlist"


def test_unreachable_is_not_reported_as_removed(
    tmp_path: Path, fake_ytdlp: Path, monkeypatch
) -> None:
    """A dropped connection is "couldn't reach YouTube", never "the
    video was removed" — the two need opposite next steps."""
    monkeypatch.setattr("clipforge.ingest._oembed_status", lambda url: "unknown")
    monkeypatch.setenv("FAKE_YTDLP_MODE", "unreachable")
    with pytest.raises(IngestError) as excinfo:
        check_youtube_available(DEAD_URL, str(fake_ytdlp))
    assert excinfo.value.code == "youtube_unreachable"
    assert "couldn't reach" in str(excinfo.value).lower()


# ------------------------------------------------------------- classifier
@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("ERROR: [youtube] X: This video is unavailable", "youtube_unavailable"),
        ("ERROR: [youtube] X: Private video", "youtube_unavailable"),
        ("ERROR: [youtube] X: This video has been removed by the uploader", "youtube_unavailable"),
        ("ERROR: [youtube] X: Sign in to confirm you're not a bot", "youtube_bot_check"),
        ("ERROR: Sign in to confirm your age", "youtube_age_gated"),
        ("ERROR: This video is not available in your country", "youtube_geo_blocked"),
        ("ERROR: Unable to download webpage: Remote end closed connection", "youtube_unreachable"),
        ("ERROR: [youtube] X: Unable to download API page: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in certificate chain", "youtube_unreachable"),
        ("ERROR: [youtube] X: some new failure nobody has seen", None),
    ],
)
def test_classify_ytdlp_error(stderr: str, code: str | None) -> None:
    assert classify_ytdlp_error(stderr) == code


# ------------------------------------------------------- bounded download
def test_stall_watchdog_kills_silent_download() -> None:
    """A subprocess producing no output is killed after the stall
    timeout — the v0.10.2 hang (silent until 3600 s) in miniature."""
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    started = time.monotonic()
    with pytest.raises(IngestError) as excinfo:
        _run_ytdlp_bounded(cmd, None, timeout=60, stall_timeout=1)
    assert time.monotonic() - started < 10
    assert excinfo.value.code == "download_stalled"
    assert "stalled" in str(excinfo.value).lower()


def test_chatty_process_does_not_trip_watchdog() -> None:
    """Steady stderr output resets the stall clock — a slow-but-alive
    download is not killed."""
    cmd = [
        sys.executable,
        "-c",
        "import sys, time\n"
        "[print(i, file=sys.stderr, flush=True) or time.sleep(0.2) for i in range(10)]",
    ]
    code, stderr = _run_ytdlp_bounded(cmd, None, timeout=60, stall_timeout=1)
    assert code == 0
    assert "9" in stderr


def test_total_timeout_is_still_a_plain_ingest_error() -> None:
    """The 3600 s last-resort guard now raises IngestError (classified),
    not a bare TimeoutError that the worker maps to "took too long"."""
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    started = time.monotonic()
    with pytest.raises(IngestError) as excinfo:
        _run_ytdlp_bounded(cmd, None, timeout=1, stall_timeout=60)
    assert time.monotonic() - started < 10
    assert excinfo.value.code == "download_timeout"


def test_download_bot_check_retries_android_client(
    tmp_path: Path, fake_ytdlp: Path, monkeypatch
) -> None:
    """A bot-blocked download is retried once on the Android client
    and then succeeds — the job is saved instead of failing."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "botcheck-download")
    info = ingest_youtube(DEAD_URL, own_content=True, out_dir=tmp_path / "uploads")
    assert info.source_kind == "youtube"
    assert info.path == tmp_path / "uploads" / f"{DEAD_ID}.mp4"
    runs = _argv_runs(_log(tmp_path))
    downloads = [run for run in runs if "-o" in run]
    assert len(downloads) == 2
    assert "youtube:player_client=android" in downloads[1]


# ------------------------------------------------------- plain-language UI
@pytest.mark.parametrize(
    ("code", "title_fragment"),
    [
        ("youtube_unavailable", "isn't available on YouTube"),
        ("youtube_bot_check", "asked to sign in"),
        ("youtube_live", "live stream"),
        ("youtube_unreachable", "Couldn't reach YouTube"),
        ("download_stalled", "stalled"),
    ],
)
def test_plain_error_mapping(code: str, title_fragment: str) -> None:
    mapped = plain_error(IngestError("probe message", code=code), "ingest")
    assert mapped["code"] == code
    assert title_fragment in mapped["title"]
    assert mapped["action"]


def test_dead_link_action_tells_user_to_try_another_link() -> None:
    mapped = plain_error(IngestError("x", code="youtube_unavailable"), "ingest")
    assert "different link" in mapped["action"]
    assert "any app" in mapped["action"]
