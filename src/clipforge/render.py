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

from clipforge.broll import BrollOverlay, build_broll_filter
from clipforge.captions import write_ass
from clipforge.highlights import Candidate
from clipforge.job_control import JobControl, popen_kwargs, wait_for_process
from clipforge.reframe import ReframePlan
from clipforge.transcribe import Word
from clipforge.workflow import ffmpeg_missing_message


def _run(
    cmd: list[str],
    *,
    timeout: int,
    tool: str,
    control: JobControl | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an ffmpeg/ffprobe command, or raise a plain-language error.

    A missing binary raises ``FileNotFoundError`` from ``subprocess``;
    without this wrapper the caller would surface a bare
    ``[Errno 2] No such file or directory`` instead of telling the user
    FFmpeg is missing and where to install it.

    With a ``control`` (v0.9.0 job queue) the process runs under
    :func:`clipforge.job_control.wait_for_process`: a user cancel kills
    the ffmpeg process instead of waiting for a 1-hour render to
    finish, and :class:`JobCancelled` is raised.
    """
    if control is None:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise RenderError(ffmpeg_missing_message(tool)) from None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **popen_kwargs(),
        )
    except FileNotFoundError:
        raise RenderError(ffmpeg_missing_message(tool)) from None
    control.register_process(proc)
    try:
        returncode = wait_for_process(proc, control, timeout=timeout)
    finally:
        control.register_process(None)
    stdout, stderr = proc.communicate()
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


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


def ffprobe_verify(path: Path, control: JobControl | None = None) -> RenderResult:
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
    proc = _run(cmd, timeout=120, tool="ffprobe", control=control)
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


def _main_video_filters(
    plan: ReframePlan | None,
    clip_start_s: float,
    source_width: int,
    source_height: int,
) -> str:
    """Crop (face-tracked when a plan is given) + scale + fps + format.

    No captions here — captions are applied after any B-roll overlay
    so they always sit on top. :func:`build_filter` appends them for
    the plain path; the B-roll path appends them to the final label.
    """
    crop_w = plan.crop_width if plan else max(1, int(round(source_height * 9 / 16)))
    crop_h = source_height
    x_expr = (
        _crop_x_expression(plan, clip_start_s) if plan else str(int((source_width - crop_w) / 2))
    )
    return (
        f"crop={crop_w}:{crop_h}:'{x_expr}':0,scale={TARGET_WIDTH}:{TARGET_HEIGHT},"
        f"fps={TARGET_FPS},format=yuv420p"
    )


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
    # The crop-x expression contains commas (if(lt(t,...),...)); it is
    # quoted inside _main_video_filters so FFmpeg's filter parser does
    # not split it into separate filters.
    filters = [
        _main_video_filters(plan, clip_start_s, source_width, source_height),
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
    broll: list[BrollOverlay] | None = None,
    control: JobControl | None = None,
) -> RenderResult:
    """Render one candidate to 1080x1920 MP4; returns verified result.

    The clip is cut by the candidate's word-index-derived start/end
    times (``clip.start_s`` / ``clip.end_s``). Captions come from
    ``words`` (clipped to the candidate span) unless ``ass_path`` is
    given explicitly.

    ``broll`` overlays licensed/source-only B-roll (see
    :mod:`clipforge.broll`) as picture-in-picture cutaways on the
    9:16 frame, via a multi-input filter-complex. It cannot be
    combined with ``blurred_background`` (the two modes both rebuild
    the frame). Each overlay must have a positive duration
    (``clip_end_s`` or ``source_end_s`` set).

    ``control`` (v0.9.0 job queue): when the user cancels the job the
    ffmpeg process is killed mid-render and :class:`JobCancelled` is
    raised instead of waiting for the render to finish.
    """
    source = Path(source)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = clip.end_s - clip.start_s
    if duration <= 0:
        raise RenderError(f"candidate has non-positive duration: {clip}")

    # Probe source dimensions if no plan was supplied.
    if plan is None:
        probe = _run(
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
            timeout=120,
            tool="ffprobe",
            control=control,
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
    ]
    if broll:
        if blurred_background:
            raise RenderError("B-roll cannot be combined with blurred_background mode")
        from clipforge.broll import validate_broll

        overlays = [
            validate_broll(
                source_type=ov.source_type,
                source_path=ov.source_path,
                declaration=ov.declaration,
                source_start_s=ov.source_start_s,
                source_end_s=ov.source_end_s,
                clip_start_s=ov.clip_start_s,
                clip_end_s=ov.clip_end_s,
                position=ov.position,
                width_frac=ov.width_frac,
                clip_duration_s=duration,
                must_exist=True,
            )
            for ov in broll
        ]
        for ov in overlays:
            if ov.duration_s() <= 0:
                raise RenderError(
                    "B-roll overlay needs a positive duration (set clip_end_s or source_end_s)"
                )
            cmd += [
                "-ss",
                f"{ov.source_start_s:.3f}",
                "-t",
                f"{ov.duration_s():.3f}",
                "-i",
                ov.source_path,
            ]
        main_filters = _main_video_filters(plan, clip.start_s, src_w, src_h)
        broll_fragment, final_label = build_broll_filter(overlays)
        chains = [f"[0:v]{main_filters}[v0]", broll_fragment]
        if ass_path:
            chains.append(f"[{final_label}]ass={_escape_filter_path(str(ass_path))}[vout]")
            final_label = "vout"
        chains.append(f"[0:a]loudnorm=I={loudness_lufs}:TP=-1.5:LRA=11[aout]")
        video_filter = ";".join(chains)
        filter_flag = "-filter_complex"
        cmd += [
            filter_flag,
            video_filter,
            "-map",
            f"[{final_label}]",
            "-map",
            "[aout]",
        ]
    else:
        cmd += [filter_flag, video_filter]
        # Plain path: audio loudness via -af. The B-roll path already
        # normalises audio inside its filter-complex ([aout]).
        cmd += ["-af", f"loudnorm=I={loudness_lufs}:TP=-1.5:LRA=11"]
    cmd += [
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
    proc = _run(cmd, timeout=3600, tool="ffmpeg", control=control)
    if proc.returncode != 0:
        raise RenderError(f"ffmpeg render failed for {out}: {proc.stderr.strip()[-800:]}")
    if not out.exists() or out.stat().st_size == 0:
        raise RenderError(f"ffmpeg produced no output at {out}")
    result = ffprobe_verify(out, control=control)
    if abs(result.duration_s - duration) > 1.5:
        raise RenderError(
            f"rendered duration {result.duration_s:.2f}s differs from "
            f"requested {duration:.2f}s by more than 1.5s"
        )
    return result
