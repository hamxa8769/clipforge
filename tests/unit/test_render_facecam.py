"""Facecam Studio render tests (v0.10.0) — the real render path.

Generates a 16:9 source locally with FFmpeg lavfi sources (no
downloaded media), renders real 1080x1920 clips with a split-screen
facecam layout and with animated captions in two styles, and verifies
with ffprobe (resolution, duration, streams) plus an inspection of
the generated ASS file. Skipped when FFmpeg is not installed.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("clipforge.render", reason="render module not available")

from clipforge.facecam import FacecamLayout  # noqa: E402
from clipforge.highlights import Candidate  # noqa: E402
from clipforge.render import RenderError, render_clip  # noqa: E402
from clipforge.transcribe import Word  # noqa: E402


def _make_source(path: Path, duration: float = 6.0) -> None:
    # Top half red, bottom half blue: a split render keeps the two
    # halves visibly distinct, a fullscreen render does not.
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=red:s=1280x360:r=30:d={duration}",
        "-f",
        "lavfi",
        "-i",
        f"color=c=blue:s=1280x360:r=30:d={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
        "-filter_complex",
        "[0:v][1:v]vstack=inputs=2[v]",
        "-map",
        "[v]",
        "-map",
        "2:a",
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


def _words() -> list[Word]:
    return [
        Word(text=text, start=0.5 + i * 0.4, end=0.5 + (i + 1) * 0.4)
        for i, text in enumerate(["this", "is", "insane", "gameplay"])
    ]


def _cand() -> Candidate:
    return Candidate(
        start_word_index=0,
        end_word_index=0,
        start_s=0.0,
        end_s=4.0,
        score=80,
        hook=5,
        hook_line="",
        reason="facecam render test",
    )


def _frame_hash(path: Path, t: float = 1.0) -> str:
    raw = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{t}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    tmp = tmp_path_factory.mktemp("facecam_render")
    src = tmp / "source.mp4"
    _make_source(src)
    return tmp, src


def test_split_render_is_1080x1920_with_captions(source):
    tmp, src = source
    out = tmp / "split.mp4"
    layout = FacecamLayout(
        mode="split",
        facecam={"x": 0.0, "y": 0.0, "w": 1.0, "h": 0.5},
        gameplay={"x": 0.0, "y": 0.5, "w": 1.0, "h": 0.5},
        split_ratio=0.5,
    )
    result = render_clip(src, _cand(), out, style="bounce", words=_words(), facecam=layout)
    assert (result.width, result.height) == (1080, 1920)
    assert result.video_codec == "h264"
    assert abs(result.duration_s - 4.0) < 1.5
    ass = out.with_suffix(".ass")
    assert ass.exists()
    text = ass.read_text(encoding="utf-8")
    assert "\\t(0,60,\\fscx" in text  # bounce pop animation
    assert "Bounce" in text


def test_pip_render_differs_from_split(source):
    tmp, src = source
    split_out = tmp / "split2.mp4"
    pip_out = tmp / "pip.mp4"
    render_clip(
        src,
        _cand(),
        split_out,
        style="bounce",
        words=_words(),
        facecam=FacecamLayout(mode="split", split_ratio=0.5),
    )
    result = render_clip(
        src,
        _cand(),
        pip_out,
        style="bounce",
        words=_words(),
        facecam=FacecamLayout(mode="pip", pip_pos="top-right"),
    )
    assert (result.width, result.height) == (1080, 1920)
    # Same source, same captions, different layout -> different pixels.
    assert _frame_hash(split_out) != _frame_hash(pip_out)


def test_karaoke_captions_burned_via_ass(source):
    tmp, src = source
    out = tmp / "karaoke.mp4"
    render_clip(
        src,
        _cand(),
        out,
        style="karaoke",
        words=_words(),
        facecam=FacecamLayout(mode="fullscreen"),
    )
    ass = out.with_suffix(".ass")
    text = ass.read_text(encoding="utf-8")
    assert "\\kf" in text
    assert "Karaoke" in text
    # Fullscreen mode has no facecam box: the whole source fills the frame.
    assert out.stat().st_size > 0


def test_facecam_rejects_conflicting_modes(source):
    tmp, src = source
    from clipforge.broll import BrollOverlay

    with pytest.raises(RenderError, match="B-roll"):
        render_clip(
            src,
            _cand(),
            tmp / "x.mp4",
            facecam=FacecamLayout(mode="split"),
            broll=[
                BrollOverlay(
                    source_type="local",
                    source_path=str(src),
                    declaration="own",
                    source_start_s=0.0,
                    source_end_s=1.0,
                    clip_start_s=0.0,
                    clip_end_s=1.0,
                    position="top-right",
                    width_frac=0.3,
                )
            ],
        )
    with pytest.raises(RenderError, match="blurred_background"):
        render_clip(
            src,
            _cand(),
            tmp / "y.mp4",
            facecam=FacecamLayout(mode="split"),
            blurred_background=True,
        )
