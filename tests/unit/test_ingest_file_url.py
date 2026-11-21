"""Unit tests for ``file://`` local sources (audit fix, v0.4.0).

``POST /jobs`` accepts a ``source_url`` for local jobs, and API clients
routinely send a ``file:///abs/path.mp4`` URL there. Before this fix the
worker passed the URL verbatim to ``Path()``, so ingest failed with the
confusing ``source file does not exist: file:/abs/path.mp4`` — the job
showed that error in the UI even though the file was right there.
``validate_local_file`` / ``ingest_local`` now unwrap ``file://`` URLs
(percent-decoded, ``localhost`` host allowed) to real paths.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clipforge.ingest import IngestError, ingest_local, validate_local_file


def _make_video(path: Path, duration: int = 2) -> Path:
    """A tiny real mp4 (testsrc) so ffprobe validation can run."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size=320x240:rate=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "10",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0 or not path.exists():
        pytest.skip(f"ffmpeg could not generate a test video: {proc.stderr}")
    return path


def test_file_url_ingests_existing_video(tmp_path: Path) -> None:
    video = _make_video(tmp_path / "sample.mp4")
    info = validate_local_file(video.as_uri())
    assert info.path == video
    assert info.duration_s == pytest.approx(2.0, abs=0.5)
    assert info.source_kind == "local"


def test_file_url_localhost_host_ingests(tmp_path: Path) -> None:
    video = _make_video(tmp_path / "sample.mp4")
    url = video.as_uri().replace("file:///", "file://localhost/")
    assert validate_local_file(url).path == video


def test_file_url_with_percent_encoded_space(tmp_path: Path) -> None:
    video = _make_video(tmp_path / "my clip.mp4")
    assert validate_local_file(video.as_uri()).path == video


def test_file_url_missing_file_names_real_path(tmp_path: Path) -> None:
    missing = tmp_path / "gone.mp4"
    with pytest.raises(IngestError) as excinfo:
        validate_local_file(missing.as_uri())
    assert "file:" not in str(excinfo.value)
    assert str(missing) in str(excinfo.value)


def test_plain_path_unchanged(tmp_path: Path) -> None:
    video = _make_video(tmp_path / "sample.mp4")
    assert validate_local_file(str(video)).path == video
    assert validate_local_file(video).path == video


def test_non_localhost_file_url_still_rejected(tmp_path: Path) -> None:
    # file://some-host/... must not silently resolve to a local path.
    with pytest.raises(IngestError):
        validate_local_file("file://some-host.example/tmp/video.mp4")


def test_ingest_local_alias_accepts_file_url(tmp_path: Path) -> None:
    video = _make_video(tmp_path / "sample.mp4")
    assert ingest_local(video.as_uri()).path == video
