"""Download file-locking fix (v0.9.2): isolated dirs, stale cleanup,
retry flags, plain-language lock errors.

The v0.9.1 failure, reported by the user 2026-11-23: a YouTube
download died on ``[Error 32] ... .part-Frag3.part -> .part-Frag3``
("Giving up after 3 retries"), then ``[Errno 2] No such file``.
The mechanism was reproduced locally with *real* yt-dlp against a
local DASH manifest (``/tmp/cf-dllock-repro/repro.py``, not committed):
with ``os.replace`` patched to raise EACCES on ``*.part`` renames —
exactly what Windows reports as WinError 32 when another process
(typically Defender real-time scanning) holds the just-written
fragment — yt-dlp printed ``Unable to rename file: [Errno 13]
Permission denied: '...test.mp4.part-Frag1.part'. Giving up after 3
retries`` and failed in 0.76 s. Its file-access retry is only 3
attempts ~10 ms apart (``file_access_retries`` default), and
ClipForge passed no retry options at all. With
``file_access_retries=10`` + a 1 s retry sleep the same 2 s transient
lock is ridden out and the download completes (``repro2.py``).

These tests drive :func:`clipforge.ingest.ingest_youtube` through a
fake yt-dlp binary (a small script that records its argv and
simulates fragment files + a finished mp4), so they run without
network or a real YouTube fetch.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from clipforge.errors import plain_error
from clipforge.ingest import (
    IngestError,
    _cleanup_stale_downloads,
    _move_finished_file,
    ingest_youtube,
    looks_like_download_lock,
)

VIDEO_URL = "https://youtu.be/CMzctRZQlo0?si=cb2aCsfgr4l30pV"
VIDEO_ID = "CMzctRZQlo0"

# The exact stderr the user's v0.9.1 job died with (paths shortened).
LOCK_STDERR = (
    "[Error 32] The process cannot access the file because it is being "
    "used by another process: 'C:\\Users\\Asad Khan\\AppData\\Roaming\\"
    "com.clipforge.app\\uploads\\CMzctRZQlo0.f616.mp4.part-Frag3.part' "
    "-> 'C:\\Users\\Asad Khan\\AppData\\Roaming\\com.clipforge.app\\"
    "uploads\\CMzctRZQlo0.f616.mp4.part-Frag3'. Giving up after 3 retries "
    "ERROR: Unable to download video: [Errno 2] No such file or directory: "
    "'C:\\Users\\Asad Khan\\AppData\\Roaming\\com.clipforge.app\\uploads\\...'"
)

FAKE_YTDLP = '''#!/usr/bin/env python3
"""Fake yt-dlp: records argv, simulates a DASH fragment download."""
import json, os, shutil, sys, time

log_path = os.environ["FAKE_YTDLP_LOG"]
fixture = os.environ["FAKE_YTDLP_FIXTURE"]
mode = os.environ.get("FAKE_YTDLP_MODE", "ok")
argv = sys.argv[1:]
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")

url = argv[-1]
video_id = "CMzctRZQlo0"  # the test URL's id
outtmpl = argv[argv.index("-o") + 1]
out_dir = os.path.dirname(outtmpl)
os.makedirs(out_dir, exist_ok=True)

if mode == "lock":
    sys.stderr.write(os.environ["FAKE_YTDLP_STDERR"])
    sys.exit(1)

if os.environ.get("FAKE_YTDLP_SLEEP"):
    time.sleep(float(os.environ["FAKE_YTDLP_SLEEP"]))

# Simulate DASH fragments the way real yt-dlp leaves them: a finished
# fragment file, plus one .part fragment still on disk.
base = os.path.join(out_dir, video_id)
open(base + ".f616.mp4.part-Frag1", "wb").write(b"frag1")
open(base + ".f616.mp4.part-Frag9.part", "wb").write(b"frag9-incomplete")
shutil.copyfile(fixture, base + ".mp4")
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
    monkeypatch.setenv("FAKE_YTDLP_STDERR", LOCK_STDERR)
    monkeypatch.setattr("clipforge.ingest.resolve_yt_dlp_path", lambda data_dir=None: str(script))
    return script


def _argv_runs(log_path: Path) -> list[list[str]]:
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def _run(tmp_path: Path, **kwargs):
    return ingest_youtube(VIDEO_URL, own_content=True, out_dir=tmp_path / "uploads", **kwargs)


# --------------------------------------------------------------------- fix
def test_download_uses_isolated_dir_per_run(tmp_path: Path, fake_ytdlp: Path) -> None:
    """Each run downloads into its own fresh dir; the mp4 lands in uploads/."""
    log = Path(os.environ["FAKE_YTDLP_LOG"])
    info1 = _run(tmp_path, job_id="job-aaa")
    info2 = _run(tmp_path, job_id="job-aaa")  # same job retried: still a fresh dir
    runs = _argv_runs(log)
    assert len(runs) == 2
    templates = [run[run.index("-o") + 1] for run in runs]
    assert templates[0] != templates[1]
    for template in templates:
        parent = Path(template).parent
        assert parent.parent == tmp_path / "uploads"
        assert parent.name.startswith(".dl-job-aaa-")
        assert template.endswith("%(id)s.%(ext)s")
    # No per-run scratch dir is left behind.
    assert [p for p in (tmp_path / "uploads").iterdir() if p.name.startswith(".dl-")] == []
    assert info1.path == tmp_path / "uploads" / f"{VIDEO_ID}.mp4"
    assert info2.path == info1.path
    assert info1.source_kind == "youtube"
    assert info1.own_content is True


def test_retry_flags_are_passed(tmp_path: Path, fake_ytdlp: Path) -> None:
    """yt-dlp gets real file-access retries with a sleep, and keeps .part."""
    _run(tmp_path)
    argv = _argv_runs(Path(os.environ["FAKE_YTDLP_LOG"]))[0]
    assert "--file-access-retries" in argv
    assert argv[argv.index("--file-access-retries") + 1] == "10"
    assert "--retry-sleep" in argv
    assert argv[argv.index("--retry-sleep") + 1] == "file_access:1"
    assert argv[argv.index("--fragment-retries") + 1] == "10"
    assert argv[argv.index("--retries") + 1] == "10"
    # Resume is NOT thrown away: --no-part would lose in-run resume.
    assert "--no-part" not in argv


def test_part_files_are_never_probed(tmp_path: Path, fake_ytdlp: Path) -> None:
    """The fake leaves a .part fragment in the download dir; ingest
    returns the finished mp4, never the fragment."""
    info = _run(tmp_path)
    assert info.path == tmp_path / "uploads" / f"{VIDEO_ID}.mp4"
    assert info.duration_s > 0
    # The fragment never reached uploads/ (the scratch dir is removed).
    leftovers = sorted(p.name for p in (tmp_path / "uploads").iterdir())
    assert leftovers == [f"{VIDEO_ID}.mp4"]


def test_concurrent_same_video_downloads_do_not_share_a_dir(
    tmp_path: Path, fake_ytdlp: Path, monkeypatch
) -> None:
    """Two runs for the same video at the same time get different dirs."""
    monkeypatch.setenv("FAKE_YTDLP_SLEEP", "0.3")
    results: list = []

    def run() -> None:
        results.append(_run(tmp_path))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(results) == 2
    templates = [run[run.index("-o") + 1] for run in _argv_runs(Path(os.environ["FAKE_YTDLP_LOG"]))]
    assert len(set(templates)) == 2


def test_stale_part_leftovers_are_cleaned_not_resumed(tmp_path: Path, fake_ytdlp: Path) -> None:
    """Old .part/.ytdl leftovers for this video are swept before the run."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    stale = [
        f"{VIDEO_ID}.f616.mp4.part-Frag3.part",
        f"{VIDEO_ID}.mp4.part",
        f"{VIDEO_ID}.mp4.ytdl",
    ]
    for name in stale:
        p = uploads / name
        p.write_bytes(b"stale")
        old = time.time() - 7200  # 2 h old: a dead run's, not a live one's
        os.utime(p, (old, old))
    info = _run(tmp_path)
    assert info.path == uploads / f"{VIDEO_ID}.mp4"
    for name in stale:
        assert not (uploads / name).exists()


def test_fresh_part_leftovers_are_left_alone(tmp_path: Path) -> None:
    """A fresh .part (an orphaned yt-dlp may still be writing it) is
    never deleted by the sweep."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    fresh = uploads / f"{VIDEO_ID}.f616.mp4.part-Frag3.part"
    fresh.write_bytes(b"maybe-live")
    removed = _cleanup_stale_downloads(uploads, VIDEO_ID)
    assert removed == []
    assert fresh.exists()


def test_cleanup_only_touches_this_videos_temp_files(tmp_path: Path) -> None:
    """The sweep never deletes finished files or another video's files."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    keep = [f"{VIDEO_ID}.mp4", "OTHERVIDEOID1.f616.mp4.part-Frag3.part"]
    for name in keep:
        p = uploads / name
        p.write_bytes(b"x")
        old = time.time() - 7200
        os.utime(p, (old, old))
    assert _cleanup_stale_downloads(uploads, VIDEO_ID) == []
    for name in keep:
        assert (uploads / name).exists()


# ------------------------------------------------------- plain-language lock
def test_cancelled_download_leaves_no_scratch_dir(
    tmp_path: Path, fake_ytdlp: Path, monkeypatch
) -> None:
    """A cancelled download removes its per-run dir (a retry starts
    fresh anyway — nothing resumes from a cancelled run)."""
    import threading as _threading

    from clipforge.errors import JobCancelled
    from clipforge.job_control import get_control

    monkeypatch.setenv("FAKE_YTDLP_SLEEP", "5")
    control = get_control("cancel-test-job")
    _threading.Timer(0.5, control.request_cancel).start()
    with pytest.raises(JobCancelled):
        ingest_youtube(
            VIDEO_URL,
            own_content=True,
            out_dir=tmp_path / "uploads",
            control=control,
            job_id="cancel-test-job",
        )
    uploads = tmp_path / "uploads"
    assert not [p for p in uploads.iterdir() if p.name.startswith(".dl-")]


def test_lock_failure_is_plain_language(tmp_path: Path, fake_ytdlp: Path, monkeypatch) -> None:
    """The user's exact stderr becomes a plain message naming the
    antivirus fix — no raw WinError, no traceback, no .part paths."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "lock")
    with pytest.raises(IngestError) as excinfo:
        _run(tmp_path)
    message = str(excinfo.value)
    assert "antivirus" in message.lower()
    assert "exclusion" in message.lower()
    assert "retry the job" in message.lower()
    assert "[Error 32]" not in message
    assert "WinError" not in message
    assert ".part-Frag3" not in message
    assert "Traceback" not in message


def test_lock_error_maps_to_lock_title_and_action(
    tmp_path: Path, fake_ytdlp: Path, monkeypatch
) -> None:
    """``plain_error`` on a lock failure tells the UI what to do."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "lock")
    with pytest.raises(IngestError) as excinfo:
        _run(tmp_path)
    mapped = plain_error(excinfo.value, "ingest")
    assert mapped["code"] == "download_file_lock"
    assert mapped["title"] == "The download was blocked by a file lock"
    assert "exclusions" in mapped["action"]
    assert "antivirus" in mapped["message"].lower()


def test_looks_like_download_lock_signatures() -> None:
    assert looks_like_download_lock(LOCK_STDERR)
    assert looks_like_download_lock(
        "ERROR: Unable to rename file: [Errno 13] Permission denied: "
        "'/x/v.mp4.part-Frag2.part'. Giving up after 3 retries"
    )
    assert looks_like_download_lock(
        "ERROR: Unable to download video: [Errno 2] No such file or directory: '/x/v.mp4.part'"
    )
    assert not looks_like_download_lock("ERROR: Video unavailable")
    assert not looks_like_download_lock("")


def test_move_finished_file_rides_out_a_brief_lock(tmp_path: Path, monkeypatch) -> None:
    """The uploads move retries a transient lock instead of failing."""
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")
    dest = tmp_path / "b.mp4"
    real_replace = os.replace
    attempts = {"n": 0}

    def flaky_replace(a, b, *args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise PermissionError(13, "Permission denied", str(a))
        return real_replace(a, b, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky_replace)
    _move_finished_file(src, dest)
    assert dest.exists()
    assert attempts["n"] == 3


def test_move_finished_file_gives_up_on_a_persistent_lock(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")
    monkeypatch.setattr(
        os,
        "replace",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")),
    )
    with pytest.raises(PermissionError):
        _move_finished_file(src, tmp_path / "b.mp4")
