"""Ingest stall retry (v0.11.0): a stalled download is retried once
automatically — in the same download dir, so yt-dlp resumes the
partial fragments it already wrote — before it fails the job.

The user's "Memes" agent job died on the first stall after 223 s
("The download stalled — no data arrived from YouTube for 180s").
These tests drive :func:`clipforge.ingest.ingest_youtube` through a
fake yt-dlp binary whose download run can stall once (silent sleep,
killed by the watchdog) and then succeed, or stall every time.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from clipforge.ingest import IngestError, ingest_youtube

LIVE_URL = "https://www.youtube.com/watch?v=M7lc1UVf-VE"

FAKE_YTDLP = '''#!/usr/bin/env python3
"""Fake yt-dlp for stall-retry tests: download runs can stall."""
import json, os, shutil, sys, time

log_path = os.environ["FAKE_YTDLP_LOG"]
fixture = os.environ["FAKE_YTDLP_FIXTURE"]
state_path = os.environ["FAKE_YTDLP_STATE"]
mode = os.environ.get("FAKE_YTDLP_MODE", "ok")
argv = sys.argv[1:]
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")

if "--simulate" in argv:
    print(json.dumps({"id": "M7lc1UVf-VE", "title": "Fake video",
                      "duration": 120, "is_live": False, "_type": "video"}))
    sys.exit(0)

# Download run: stall modes sleep silently (no output at all, so the
# watchdog kills the process). "stall-once" stalls only the first
# download run (tracked in the state file), then succeeds.
if mode in {"stall-once", "stall-always"}:
    try:
        count = int(open(state_path, encoding="utf-8").read().strip() or "0")
    except OSError:
        count = 0
    with open(state_path, "w", encoding="utf-8") as fh:
        fh.write(str(count + 1))
    if mode == "stall-always" or count == 0:
        time.sleep(30)
        sys.exit(0)
outtmpl = argv[argv.index("-o") + 1]
out_dir = os.path.dirname(outtmpl)
os.makedirs(out_dir, exist_ok=True)
shutil.copyfile(fixture, os.path.join(out_dir, "M7lc1UVf-VE.mp4"))
sys.exit(0)
'''


@pytest.fixture()
def fixture_video(tmp_path: Path) -> Path:
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
    # The real stall timeout is 180 s; 1 s keeps the test fast while
    # still exercising the watchdog + retry path.
    monkeypatch.setattr("clipforge.ingest.YTDLP_STALL_TIMEOUT_S", 1)
    return script


def _argv_runs(tmp_path: Path) -> list[list[str]]:
    return [
        json.loads(line)
        for line in (tmp_path / "argv.log").read_text().splitlines()
        if line.strip()
    ]


def _download_runs(tmp_path: Path) -> list[list[str]]:
    return [a for a in _argv_runs(tmp_path) if "--simulate" not in a]


def test_stall_once_is_retried_and_succeeds(tmp_path: Path, fake_ytdlp: Path, monkeypatch) -> None:
    """First download stalls → one automatic retry → the job succeeds.

    The retry reuses the same download dir (same ``-o`` template), so
    yt-dlp resumes the partial fragments instead of starting over.
    """
    monkeypatch.setenv("FAKE_YTDLP_MODE", "stall-once")
    started = time.monotonic()
    info = ingest_youtube(LIVE_URL, own_content=True, out_dir=tmp_path / "uploads")
    assert time.monotonic() - started < 30
    assert info.source_kind == "youtube"
    downloads = _download_runs(tmp_path)
    assert len(downloads) == 2
    assert downloads[0][downloads[0].index("-o") + 1] == downloads[1][downloads[1].index("-o") + 1]


def test_stall_twice_fails_and_says_it_was_retried(
    tmp_path: Path, fake_ytdlp: Path, monkeypatch
) -> None:
    """Two stalls → the job fails, and the message says the retry
    already happened, so the user is not told to "just retry" what
    the app already tried."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "stall-always")
    started = time.monotonic()
    with pytest.raises(IngestError) as excinfo:
        ingest_youtube(LIVE_URL, own_content=True, out_dir=tmp_path / "uploads")
    assert time.monotonic() - started < 30
    assert excinfo.value.code == "download_stalled"
    assert "retried once" in str(excinfo.value)
    assert len(_download_runs(tmp_path)) == 2


def test_plain_error_action_names_the_automatic_retry() -> None:
    """The job-page action for a stall no longer pretends no retry
    happened (v0.11.0): ingest retries once before failing."""
    from clipforge.errors import plain_error

    mapped = plain_error(
        IngestError("The download stalled — no data arrived.", code="download_stalled")
    )
    assert mapped["code"] == "download_stalled"
    assert "retried" in mapped["action"]
