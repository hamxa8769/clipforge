"""FFmpeg rendering for ClipForge.

One clip -> one 1080x1920 H.264 30 fps MP4 with burned ASS captions and
audio loudness-normalised to -14 LUFS (report §2 stages 6–8).

The crop is face-tracked when a :class:`ReframePlan` is supplied: the
crop ``x`` is an FFmpeg expression over ``t`` built from the sampled
crop points (piecewise-constant per sample interval), so the window
follows the speaker without re-encoding the whole clip per frame.
Without a plan the crop is centred.

Blurred-background mode (slides / screen recordings): the full frame is
scaled, blurred and stretched to 1080x1920 behind a centred 9:16
foreground layer instead of cropping the sides away.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from clipforge.captions import write_ass
from clipforge.highlights import Candidate
from clipforge.reframe import ReframePlan
from clipforge.transcribe import Word

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
TARGET_FPS = 30
TARGET_LUFS = -14.0


@dataclass
class RenderResult:
    """Verified output of one render."""

    path: Path
    width: int
    height: int
    duration_s: float
    video_codec: str
    size_bytes: int

    def to_dict(self) -> dict:
        d = asdict(self)
        d["path"] = str(self.path)
        return d


class RenderError(RuntimeError):
    """Raised when FFmpeg fails or verification does not match."""


def ffprobe_verify(path: Path) -> RenderResult:
    """Probe a rendered file and verify 1080x1920; raises on mismatch."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=width,height,codec_name,codec_type",
        "-show_entries",
        "format=duration,size",
        "-of",
        "json",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RenderError(f"ffprobe failed for {path}: {proc.stderr.strip()}")
    info = json.loads(proc.stdout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise RenderError(f"no video stream in rendered file {path}")
    width, height = int(video["width"]), int(video["height"])
    if (width, height) != (TARGET_WIDTH, TARGET_HEIGHT):
        raise RenderError(
            f"rendered {path} is {width}x{height}, expected {TARGET_WIDTH}x{TARGET_HEIGHT}"
        )
    duration = float(info.get("format", {}).get("duration", 0) or 0)
    return RenderResult(
        path=path,
        width=width,
        height=height,
        duration_s=duration,
        video_codec=str(video.get("codec_name", "unknown")),
        size_bytes=int(info.get("format", {}).get("size", 0) or 0),
    )


def _escape_filter_path(path: str) -> str:
    """Escape a path for use inside an FFmpeg filter argument."""
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _crop_x_expression(plan: ReframePlan, clip_start_s: float) -> str:
    """Piecewise crop-x expression over clip-local time ``t``.

    FFmpeg's crop filter evaluates ``x`` per frame; ``t`` is the
    timestamp within the (already -ss trimmed) input. Points are
    relative to the clip start.
    """
    points = plan.crop_points
    if not points:
        return str(int((plan.source_width - plan.crop_width) / 2))
    local = [(max(0.0, p.t - clip_start_s), int(round(p.x))) for p in points]
    local = sorted(set(local))
    if len(local) == 1:
        return str(local[0][1])
    expr = str(local[-1][1])
    for t, x in reversed(local[:-1]):
        expr = f"if(lt(t,{t:.2f}),{x},{expr})"
    return expr


def build_filter(
    plan: ReframePlan | None,
    clip_start_s: float,
    ass_path: str | None,
    blurred_background: bool,
    source_width: int,
    source_height: int,
) -> str:
    """Build the video filtergraph. Pure function (no FFmpeg call)."""
    crop_w = plan.crop_width if plan else max(1, int(round(source_height * 9 / 16)))
    crop_h = source_height
    if blurred_background:
        # Filter-complex graph (labelled chains, joined with ';'):
        # background = full frame, scaled to fill, heavily blurred;
        # foreground = centred 9:16 crop scaled to 1080x1920, overlaid.
        tail = f"fps={TARGET_FPS},format=yuv420p"
        if ass_path:
            tail += f",ass={_escape_filter_path(ass_path)}"
        return ";".join(
            [
                "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,gblur=sigma=40[bg]",
                f"[0:v]crop={crop_w}:{crop_h}:"
                f"{int((source_width - crop_w) / 2)}:0,"
                f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}[fg]",
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{tail}",
            ]
        )
    x_expr = (
        _crop_x_expression(plan, clip_start_s) if plan else str(int((source_width - crop_w) / 2))
    )
    # The x expression contains commas (if(lt(t,...),...)); quote it so
    # FFmpeg's filter parser does not split it into separate filters.
    filters = [
        f"crop={crop_w}:{crop_h}:'{x_expr}':0,scale={TARGET_WIDTH}:{TARGET_HEIGHT}",
        f"fps={TARGET_FPS}",
        "format=yuv420p",
    ]
    if ass_path:
        filters.append(f"ass={_escape_filter_path(ass_path)}")
    return ",".join(filters)


def render_clip(
    source: str | Path,
    clip: Candidate,
    out_path: str | Path,
    style: str = "bold-pop",
    *,
    words: list[Word] | None = None,
    plan: ReframePlan | None = None,
    blurred_background: bool = False,
    ass_path: str | Path | None = None,
    loudness_lufs: float = TARGET_LUFS,
) -> RenderResult:
    """Render one candidate to 1080x1920 MP4; returns verified result.

    The clip is cut by the candidate's word-index-derived start/end
    times (``clip.start_s`` / ``clip.end_s``). Captions come from
    ``words`` (clipped to the candidate span) unless ``ass_path`` is
    given explicitly.
    """
    source = Path(source)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = clip.end_s - clip.start_s
    if duration <= 0:
        raise RenderError(f"candidate has non-positive duration: {clip}")

    # Probe source dimensions if no plan was supplied.
    if plan is None:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        dims = json.loads(probe.stdout)["streams"][0]
        src_w, src_h = int(dims["width"]), int(dims["height"])
    else:
        src_w, src_h = plan.source_width, plan.source_height

    if ass_path is None and words:
        from clipforge.captions import clip_words

        clip_ws = clip_words(words, clip.start_s, clip.end_s)
        if clip_ws:
            ass_file = out.with_suffix(".ass")
            write_ass(clip_ws, ass_file, style_name=style, start_offset_s=clip.start_s)
            ass_path = ass_file

    video_filter = build_filter(
        plan,
        clip.start_s,
        str(ass_path) if ass_path else None,
        blurred_background,
        src_w,
        src_h,
    )
    filter_flag = "-filter_complex" if blurred_background else "-vf"
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{clip.start_s:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source),
        filter_flag,
        video_filter,
        "-af",
        f"loudnorm=I={loudness_lufs}:TP=-1.5:LRA=11",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise RenderError(f"ffmpeg render failed for {out}: {proc.stderr.strip()[-800:]}")
    if not out.exists() or out.stat().st_size == 0:
        raise RenderError(f"ffmpeg produced no output at {out}")
    result = ffprobe_verify(out)
    if abs(result.duration_s - duration) > 1.5:
        raise RenderError(
            f"rendered duration {result.duration_s:.2f}s differs from "
            f"requested {duration:.2f}s by more than 1.5s"
        )
    return result
