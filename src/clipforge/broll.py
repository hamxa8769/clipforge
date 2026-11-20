"""B-roll support for ClipForge (Phase 2) — licensed / source-only.

Rule (repo policy, see README / docs/compliance.md): ClipForge only
processes content the user owns or holds the rights to. B-roll may
therefore come from exactly two places:

* ``source_video`` — a segment cut from the clip's own source video
  (the file the job was created from); or
* ``uploaded_licensed`` — a file the user uploaded themselves, with a
  written source/rights declaration.

URLs, third-party downloads, and anything fetched from the web are
rejected at validation time and never stored. There is no downloader
here on purpose.

The render wiring is real, not a stub: :func:`build_broll_filter`
produces the FFmpeg filter-complex fragment that overlays the B-roll
as picture-in-picture on the 9:16 frame, and
:func:`render_clip` (in :mod:`clipforge.render`) accepts B-roll
overlays and builds the multi-input command. A clip-level overlay
spec (``overlay_spec``) is also exposed for the API / review UI so a
frontend can show where the B-roll sits without running FFmpeg.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

SOURCE_VIDEO = "source_video"
UPLOADED_LICENSED = "uploaded_licensed"
SOURCE_TYPES = (SOURCE_VIDEO, UPLOADED_LICENSED)

POSITIONS = ("picture-in-picture", "top-right", "bottom-right", "fullscreen")


@dataclass
class BrollOverlay:
    """One validated B-roll overlay on a clip's timeline."""

    source_type: str
    source_path: str
    declaration: str
    source_start_s: float = 0.0
    source_end_s: float | None = None
    clip_start_s: float = 0.0  # when the overlay appears in the clip
    clip_end_s: float | None = None
    position: str = "picture-in-picture"
    width_frac: float = 0.45  # overlay width as a fraction of 1080

    def duration_s(self) -> float:
        """Overlay duration on the clip timeline (seconds)."""
        if self.clip_end_s is not None:
            return max(0.0, self.clip_end_s - self.clip_start_s)
        if self.source_end_s is not None:
            return max(0.0, self.source_end_s - self.source_start_s)
        return 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class BrollError(ValueError):
    """Raised when a B-roll asset fails the source/rights validation."""


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https", "ftp"} and bool(parsed.netloc)


def validate_broll(
    *,
    source_type: str,
    source_path: str,
    declaration: str,
    source_start_s: float = 0.0,
    source_end_s: float | None = None,
    clip_start_s: float = 0.0,
    clip_end_s: float | None = None,
    position: str = "picture-in-picture",
    width_frac: float = 0.45,
    clip_duration_s: float | None = None,
    must_exist: bool = False,
) -> BrollOverlay:
    """Validate a B-roll asset; returns a :class:`BrollOverlay` or raises.

    Rejections: unknown ``source_type``; any URL as ``source_path``;
    missing/empty rights ``declaration``; non-positive or inverted
    time ranges; overlay outside the clip (when ``clip_duration_s``
    is given); unknown ``position``; ``width_frac`` outside (0, 1].
    ``must_exist=True`` additionally requires the file to exist on
    disk (used by the render path, not by the API, which may register
    an upload that lands a moment later).
    """
    if source_type not in SOURCE_TYPES:
        raise BrollError(
            f"source_type must be one of {SOURCE_TYPES}, got {source_type!r}. "
            "B-roll comes only from the source video or a user-uploaded licensed file."
        )
    path = (source_path or "").strip()
    if not path:
        raise BrollError("source_path is required")
    if _looks_like_url(path):
        raise BrollError(
            f"URLs are not accepted as B-roll sources: {path!r}. "
            "Use a segment of the source video or upload a licensed file instead."
        )
    if not (declaration or "").strip():
        raise BrollError(
            "A source/rights declaration is required for B-roll "
            "(e.g. 'Cut from my own source video' or 'Licensed stock, licence #…')."
        )
    if source_start_s < 0 or clip_start_s < 0:
        raise BrollError("start times must be >= 0")
    if source_end_s is not None and source_end_s <= source_start_s:
        raise BrollError("source_end_s must be greater than source_start_s")
    if clip_end_s is not None and clip_end_s <= clip_start_s:
        raise BrollError("clip_end_s must be greater than clip_start_s")
    if position not in POSITIONS:
        raise BrollError(f"position must be one of {POSITIONS}, got {position!r}")
    if not 0.0 < width_frac <= 1.0:
        raise BrollError(f"width_frac must be in (0, 1], got {width_frac!r}")
    if clip_duration_s is not None and clip_start_s >= clip_duration_s:
        raise BrollError(f"clip_start_s {clip_start_s}s is outside the clip ({clip_duration_s}s)")
    if clip_duration_s is not None and clip_end_s is not None and clip_end_s > clip_duration_s:
        raise BrollError(f"clip_end_s {clip_end_s}s exceeds the clip duration ({clip_duration_s}s)")
    if must_exist and not os.path.isfile(path):
        raise BrollError(f"B-roll file not found on disk: {path!r}")
    return BrollOverlay(
        source_type=source_type,
        source_path=path,
        declaration=declaration.strip(),
        source_start_s=source_start_s,
        source_end_s=source_end_s,
        clip_start_s=clip_start_s,
        clip_end_s=clip_end_s,
        position=position,
        width_frac=width_frac,
    )


def overlay_spec(overlay: BrollOverlay, clip_duration_s: float) -> dict:
    """Clip-level overlay spec for the API / review UI (no FFmpeg needed).

    Positions are given in 1080x1920 output pixels: picture-in-picture
    sits bottom-right inside the Meta safe zones; ``fullscreen`` covers
    the frame (used for a full cutaway); ``top-right`` / ``bottom-right``
    are corner variants of the same size.
    """
    width = int(round(1080 * overlay.width_frac))
    # B-roll is assumed 16:9; height follows the overlay width.
    height = int(round(width * 9 / 16))
    positions = {
        "picture-in-picture": (1080 - width - 48, 1920 - height - 500),
        "bottom-right": (1080 - width - 48, 1920 - height - 120),
        "top-right": (1080 - width - 48, 300),
        "fullscreen": (0, 0),
    }
    x, y = positions[overlay.position]
    if overlay.position == "fullscreen":
        width, height = 1080, 1920
    return {
        **overlay.to_dict(),
        "output": {"x": x, "y": y, "width": width, "height": height},
        "clip_duration_s": clip_duration_s,
        "overlay_duration_s": overlay.duration_s(),
    }


def build_broll_filter(
    overlays: list[BrollOverlay],
    main_label: str = "v0",
    target_width: int = 1080,
    target_height: int = 1920,
) -> tuple[str, str]:
    """FFmpeg filter-complex fragment that overlays B-roll on the main video.

    ``main_label`` is the label of the already-cropped/scaled 9:16 main
    stream (input 0). Each overlay becomes input ``i+1``. Returns
    ``(filter_fragment, final_label)`` — the fragment ends with the
    final video label, ready to be joined with the main filter chain
    (see :func:`clipforge.render.render_clip`).

    The overlay is enabled only between ``clip_start_s`` and
    ``clip_end_s`` (or for the source-segment duration when no clip end
    is given), so the B-roll is a cutaway, not a permanent watermark.
    """
    if not overlays:
        return "", main_label
    chains: list[str] = []
    current = main_label
    for i, ov in enumerate(overlays):
        width = target_width if ov.position == "fullscreen" else int(round(1080 * ov.width_frac))
        height = target_height if ov.position == "fullscreen" else int(round(width * 9 / 16))
        b_label = f"b{i}"
        chains.append(
            f"[{i + 1}:v]scale={width}:{height},setsar=1,fps=30,format=yuv420p[{b_label}]"
        )
        x, y = {
            "picture-in-picture": (f"{target_width - width}-48", f"{target_height - height}-500"),
            "bottom-right": (f"{target_width - width}-48", f"{target_height - height}-120"),
            "top-right": (f"{target_width - width}-48", "300"),
            "fullscreen": ("0", "0"),
        }[ov.position]
        end = ov.clip_end_s if ov.clip_end_s is not None else ov.clip_start_s + ov.duration_s()
        enable = f"between(t,{ov.clip_start_s:.3f},{end:.3f})"
        out_label = f"v{i + 1}"
        chains.append(f"[{current}][{b_label}]overlay={x}:{y}:enable='{enable}'[{out_label}]")
        current = out_label
    return ";".join(chains), current


__all__ = [
    "BrollOverlay",
    "BrollError",
    "SOURCE_VIDEO",
    "UPLOADED_LICENSED",
    "SOURCE_TYPES",
    "POSITIONS",
    "validate_broll",
    "overlay_spec",
    "build_broll_filter",
]
