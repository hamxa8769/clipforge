"""B-roll render tests (Phase 2) — the FFmpeg picture-in-picture path.

Generates source + B-roll clips locally with FFmpeg lavfi sources
(no downloaded media) and renders a real 1080x1920 clip with a B-roll
overlay, verifying dimensions and duration. Skipped when FFmpeg is
not installed.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

pytest.importorskip("clipforge.render", reason="render module not available")

from clipforge.broll import BrollOverlay  # noqa: E402
from clipforge.highlights import Candidate  # noqa: E402
from clipforge.render import RenderError, render_clip  # noqa: E402


def _make_clip(path, lavfi_video: str, duration: float) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        lavfi_video,
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    tmp = tmp_path_factory.mktemp("broll_render")
    source = tmp / "source.mp4"
    broll = tmp / "broll.mp4"
    _make_clip(source, "testsrc2=size=1280x720:rate=30:duration=6", 6.0)
    _make_clip(broll, "color=c=red:s=640x360:r=30:d=6", 6.0)
    return tmp, source, broll


def _candidate() -> Candidate:
    return Candidate(
        start_word_index=0,
        end_word_index=1,
        start_s=0.0,
        end_s=6.0,
        score=80.0,
        hook=8,
        hook_line="test hook",
        reason="test",
    )


def test_render_clip_with_broll_overlay(videos):
    tmp, source, broll_path = videos
    out = tmp / "with_broll.mp4"
    result = render_clip(
        source,
        _candidate(),
        out,
        broll=[
            BrollOverlay(
                source_type="source_video",
                source_path=str(broll_path),
                declaration="test fixture, generated locally",
                source_start_s=0.0,
                source_end_s=3.0,
                clip_start_s=1.0,
                clip_end_s=4.0,
            )
        ],
    )
    assert result.width == 1080 and result.height == 1920
    assert result.video_codec == "h264"
    assert abs(result.duration_s - 6.0) < 1.5
    assert result.size_bytes > 0


def test_render_clip_without_broll_still_works(videos):
    tmp, source, _ = videos
    out = tmp / "plain.mp4"
    result = render_clip(source, _candidate(), out)
    assert (result.width, result.height) == (1080, 1920)
    assert abs(result.duration_s - 6.0) < 1.5


def test_render_clip_broll_rejects_zero_duration_overlay(videos):
    tmp, source, broll_path = videos
    with pytest.raises(RenderError, match="positive duration"):
        render_clip(
            source,
            _candidate(),
            tmp / "bad.mp4",
            broll=[
                BrollOverlay(
                    source_type="source_video",
                    source_path=str(broll_path),
                    declaration="test",
                    clip_start_s=1.0,  # no clip_end_s / source_end_s -> 0s
                )
            ],
        )


def test_render_clip_broll_rejects_url_source(videos):
    tmp, source, _ = videos
    with pytest.raises(Exception, match="URLs are not accepted"):
        render_clip(
            source,
            _candidate(),
            tmp / "bad2.mp4",
            broll=[
                BrollOverlay(
                    source_type="uploaded_licensed",
                    source_path="https://example.com/stock.mp4",
                    declaration="licensed",
                    clip_start_s=1.0,
                    clip_end_s=3.0,
                )
            ],
        )
