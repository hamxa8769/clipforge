"""Facecam Studio layouts for ClipForge (v0.10.0).

A facecam layout describes how a (usually 16:9) source is turned into a
1080x1920 vertical clip with the streamer's camera and the gameplay
arranged on the frame — the same job ClipNova's ``render.py`` does with
its ``split`` / ``split_half`` / ``gameplay`` / ``custom`` layouts, but
re-implemented here in ClipForge's architecture:

* Boxes are **normalised** (fractions of the source frame, 0..1), not
  source pixels, so a preset saved for one video still applies to
  another video of a different resolution. They are converted to
  pixels at render time from the probed source dimensions.
* :func:`build_facecam_filter` is a pure function (no FFmpeg call) so
  the filter graph is unit-testable; :func:`clipforge.render.render_clip`
  is the only place that runs FFmpeg with it.
* Per-creator presets live in ``<data_dir>/facecam_presets.json``
  (plain JSON, no secrets) via :class:`FacecamPresetStore`.

Modes
-----
``split``      Facecam on top, gameplay below, glowing divider line
               (ClipNova's ``split_half`` / ``custom`` split).
``pip``        Gameplay fills the frame, facecam as a corner overlay
               (ClipNova's ``gameplay``).
``zoom``       Zoomed 16:9 foreground over a blurred full-frame
               background (ClipNova's ``zoom``).
``blur_fill``  16:9 source over a blurred background (ClipNova's
               ``blur``; ClipForge already had this as
               ``blurred_background`` — here it is a first-class
               facecam mode so presets can select it).
``fullscreen`` Gameplay fills the frame, no camera (ClipNova's
               ``crop``).

ClipNova also auto-detects the facecam box from the video itself
(straight-border + stillness analysis in its ``facecam.py``). ClipForge
v0.10.0 does **not** do that: the boxes come from the Studio's draggable
9:16 canvas (or a saved preset). Auto-detection is a documented gap,
not a silent fallback.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from clipforge.render import TARGET_HEIGHT, TARGET_WIDTH

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

LAYOUT_MODES = ("split", "pip", "zoom", "blur_fill", "fullscreen")
PIP_POSITIONS = ("top-right", "top-left", "bottom-left", "bottom-right")

MODE_LABELS = {
    "split": "Split screen — facecam top, gameplay bottom",
    "pip": "Picture-in-picture — gameplay full screen, facecam in a corner",
    "zoom": "Zoom — gameplay zoomed over a blurred background",
    "blur_fill": "Blur fill — 16:9 video over a blurred background",
    "fullscreen": "Fullscreen — gameplay fills the frame, no facecam",
}

PIP_MARGIN = 26  # px from the frame edge, as in the reference layout
PIP_BOTTOM_LIFT = 240  # bottom PiP sits this far above the bottom edge (above the UI)
DIVIDER_DARK_H = 6
DIVIDER_GLOW_H = 2
ZOOM_FG_H = 1000  # zoom mode foreground height, as in the reference layout
BLUR_SIGMA = 40

DEFAULT_CAPTION_Y = 1120  # ClipForge safe-band caption line (see captions.py)


class FacecamError(ValueError):
    """Raised for an invalid layout, box or preset name."""


@dataclass
class Box:
    """A crop box as fractions of the source frame (0..1).

    ``x`` / ``y`` are the top-left corner, ``w`` / ``h`` the size.
    The full frame is ``Box(0, 0, 1, 1)``.
    """

    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0

    def clamp(self) -> Box:
        """Clamp into the frame; keep a usable minimum size."""
        x = min(max(0.0, float(self.x)), 0.95)
        y = min(max(0.0, float(self.y)), 0.95)
        w = min(max(0.05, float(self.w)), 1.0 - x)
        h = min(max(0.05, float(self.h)), 1.0 - y)
        return Box(x=x, y=y, w=w, h=h)

    def to_pixels(self, src_w: int, src_h: int) -> tuple[int, int, int, int]:
        """(x, y, w, h) in source pixels, clamped inside the frame."""
        box = self.clamp()
        x = int(round(box.x * src_w))
        y = int(round(box.y * src_h))
        w = max(20, min(int(round(box.w * src_w)), src_w - x))
        h = max(20, min(int(round(box.h * src_h)), src_h - y))
        return x, y, w, h

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, raw: object) -> Box:
        if not isinstance(raw, dict):
            raise FacecamError(f"facecam box must be an object, got {raw!r}")
        try:
            box = cls(
                x=float(raw.get("x", 0.0)),
                y=float(raw.get("y", 0.0)),
                w=float(raw.get("w", 1.0)),
                h=float(raw.get("h", 1.0)),
            )
        except (TypeError, ValueError) as exc:
            raise FacecamError(f"facecam box has non-numeric fields: {raw!r}") from exc
        return box.clamp()


@dataclass
class FacecamLayout:
    """One Facecam Studio layout (JSON-serialisable)."""

    mode: str = "split"
    facecam: Box = field(default_factory=lambda: Box(x=0.0, y=0.0, w=1.0, h=0.35))
    gameplay: Box = field(default_factory=Box)
    split_ratio: float = 0.35  # facecam share of the height in split mode
    pip_pos: str = "top-right"
    pip_size_frac: float = 0.35  # PiP width as a fraction of the 1080 output
    divider: bool = True
    caption_y: int | None = None  # override; None = layout default

    def __post_init__(self) -> None:
        # Accept plain dicts for the boxes (JSON-shaped construction),
        # so callers need not import Box.
        if isinstance(self.facecam, dict):
            self.facecam = Box.from_dict(self.facecam)
        if isinstance(self.gameplay, dict):
            self.gameplay = Box.from_dict(self.gameplay)

    def validate(self) -> FacecamLayout:
        if self.mode not in LAYOUT_MODES:
            raise FacecamError(
                f"unknown facecam mode {self.mode!r}; choose from {sorted(LAYOUT_MODES)}"
            )
        if self.pip_pos not in PIP_POSITIONS:
            raise FacecamError(
                f"unknown pip position {self.pip_pos!r}; choose from {sorted(PIP_POSITIONS)}"
            )
        ratio = float(self.split_ratio)
        if not 0.20 <= ratio <= 0.80:
            raise FacecamError(f"split_ratio must be 0.20..0.80, got {ratio}")
        size = float(self.pip_size_frac)
        if not 0.10 <= size <= 0.60:
            raise FacecamError(f"pip_size_frac must be 0.10..0.60, got {size}")
        if self.caption_y is not None and not 0 <= int(self.caption_y) <= TARGET_HEIGHT:
            raise FacecamError(f"caption_y must be 0..{TARGET_HEIGHT}, got {self.caption_y}")
        self.facecam = self.facecam.clamp()
        self.gameplay = self.gameplay.clamp()
        return self

    def to_dict(self) -> dict:
        d = asdict(self)
        d["facecam"] = self.facecam.to_dict()
        d["gameplay"] = self.gameplay.to_dict()
        return d

    @classmethod
    def from_dict(cls, raw: object) -> FacecamLayout:
        if raw is None:
            return cls().validate()
        if not isinstance(raw, dict):
            raise FacecamError(f"facecam layout must be an object, got {raw!r}")
        try:
            layout = cls(
                mode=str(raw.get("mode", "split")),
                facecam=(
                    Box.from_dict(raw["facecam"])
                    if raw.get("facecam") is not None
                    else cls().facecam
                ),
                gameplay=(
                    Box.from_dict(raw["gameplay"])
                    if raw.get("gameplay") is not None
                    else cls().gameplay
                ),
                split_ratio=float(raw.get("split_ratio", 0.35)),
                pip_pos=str(raw.get("pip_pos", "top-right")),
                pip_size_frac=float(raw.get("pip_size_frac", 0.35)),
                divider=bool(raw.get("divider", True)),
                caption_y=(
                    int(raw["caption_y"])
                    if raw.get("caption_y") is not None
                    else None
                ),
            )
        except FacecamError:
            raise
        except (TypeError, ValueError) as exc:
            raise FacecamError(f"invalid facecam layout: {raw!r}") from exc
        return layout.validate()


# ---------------------------------------------------------------------------
# Geometry (pure)
# ---------------------------------------------------------------------------


def _even(n: int) -> int:
    return max(2, int(round(n / 2)) * 2)


@dataclass
class LayoutGeometry:
    """Pixel geometry for one layout on one source size (pure, testable)."""

    facecam_px: tuple[int, int, int, int]  # x, y, w, h in source pixels
    gameplay_px: tuple[int, int, int, int]
    cam_h: int  # split mode: facecam band height in the output
    game_h: int  # split mode: gameplay band height in the output
    pip_w: int  # pip mode: overlay width in the output
    pip_h: int  # pip mode: overlay height in the output
    pip_x: int  # pip mode: overlay left in the output
    pip_y: int  # pip mode: overlay top in the output
    caption_y: int  # caption line in the 1080x1920 output


def layout_geometry(layout: FacecamLayout, src_w: int, src_h: int) -> LayoutGeometry:
    """Pixel geometry for ``layout`` on a ``src_w`` x ``src_h`` source."""
    layout = layout.validate()
    facecam_px = layout.facecam.to_pixels(src_w, src_h)
    gameplay_px = layout.gameplay.to_pixels(src_w, src_h)
    cam_h = _even(int(round(TARGET_HEIGHT * layout.split_ratio)))
    game_h = TARGET_HEIGHT - cam_h
    pip_w = _even(int(round(TARGET_WIDTH * layout.pip_size_frac)))
    _fx, _fy, fw, fh = facecam_px
    pip_h = _even(max(2, int(round(pip_w * fh / max(1, fw)))))
    if layout.pip_pos == "bottom-left":
        pip_x, pip_y = PIP_MARGIN, TARGET_HEIGHT - pip_h - PIP_BOTTOM_LIFT
    elif layout.pip_pos == "bottom-right":
        pip_x, pip_y = TARGET_WIDTH - pip_w - PIP_MARGIN, TARGET_HEIGHT - pip_h - PIP_BOTTOM_LIFT
    elif layout.pip_pos == "top-left":
        pip_x, pip_y = PIP_MARGIN, PIP_MARGIN
    else:  # top-right
        pip_x, pip_y = TARGET_WIDTH - pip_w - PIP_MARGIN, PIP_MARGIN
    if layout.caption_y is not None:
        caption_y = int(layout.caption_y)
    elif layout.mode == "split":
        # Captions sit on the gameplay half, below the divider.
        caption_y = cam_h + int(game_h * 0.42)
    elif layout.mode == "pip" and layout.pip_pos.startswith("bottom"):
        # The PiP covers the lower band: captions move above it.
        caption_y = max(300, pip_y - 180)
    else:
        caption_y = DEFAULT_CAPTION_Y
    return LayoutGeometry(
        facecam_px=facecam_px,
        gameplay_px=gameplay_px,
        cam_h=cam_h,
        game_h=game_h,
        pip_w=pip_w,
        pip_h=pip_h,
        pip_x=pip_x,
        pip_y=pip_y,
        caption_y=caption_y,
    )


# ---------------------------------------------------------------------------
# Filter graph (pure — no FFmpeg call)
# ---------------------------------------------------------------------------


def _escape_filter_path(path: str) -> str:
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_facecam_filter(
    layout: FacecamLayout,
    src_w: int,
    src_h: int,
    ass_path: str | None = None,
) -> tuple[str, str]:
    """Build the ffmpeg filter-complex for ``layout``.

    Returns ``(filter_complex, final_label)``; the caller maps
    ``[final_label]`` to video and adds the audio chain itself. The
    graph always ends at 1080x1920, 30 fps is applied by the caller
    (``fps=`` lives on the final chain, as in ``render.build_filter``).

    Every branch is a ``scale`` with
    ``force_original_aspect_ratio=increase`` + ``crop`` so the output
    is exactly filled — no black bars — and ``setsar=1`` so players do
    not stretch it.
    """
    layout = layout.validate()
    geo = layout_geometry(layout, src_w, src_h)
    fx, fy, fw, fh = geo.facecam_px
    gx, gy, gw, gh = geo.gameplay_px
    w, h = TARGET_WIDTH, TARGET_HEIGHT

    if layout.mode == "split":
        divider = ""
        if layout.divider:
            divider = (
                f",drawbox=x=0:y={geo.cam_h - DIVIDER_DARK_H // 2}:w={w}:h={DIVIDER_DARK_H}"
                f":color=0x141210:t=fill"
                f",drawbox=x=0:y={geo.cam_h - 1}:w={w}:h={DIVIDER_GLOW_H}"
                f":color=0x00F0FF@0.9:t=fill"
            )
        graph = (
            f"[0:v]split=2[cam][game];"
            f"[cam]crop={fw}:{fh}:{fx}:{fy},"
            f"scale={w}:{geo.cam_h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{geo.cam_h},setsar=1[f];"
            f"[game]crop={gw}:{gh}:{gx}:{gy},"
            f"scale={w}:{geo.game_h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{geo.game_h},setsar=1[g];"
            f"[f][g]vstack=inputs=2{divider}[v0]"
        )
    elif layout.mode == "pip":
        graph = (
            f"[0:v]split=2[base][fc];"
            f"[base]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1[b];"
            f"[fc]crop={fw}:{fh}:{fx}:{fy},"
            f"scale={geo.pip_w}:{geo.pip_h},setsar=1[p];"
            f"[b][p]overlay={geo.pip_x}:{geo.pip_y}[v0]"
        )
    elif layout.mode == "zoom":
        graph = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},gblur=sigma={BLUR_SIGMA},"
            f"eq=brightness=-0.14:saturation=1.15[bg2];"
            f"[fg]crop={gw}:{gh}:{gx}:{gy},"
            f"scale={w}:{ZOOM_FG_H}:force_original_aspect_ratio=increase,"
            f"crop={w}:{ZOOM_FG_H},setsar=1[fg2];"
            f"[bg2][fg2]overlay=0:(H-h)/2[v0]"
        )
    elif layout.mode == "blur_fill":
        crop_w = max(1, int(round(src_h * 9 / 16)))
        crop_x = int((src_w - crop_w) / 2)
        graph = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},gblur=sigma={BLUR_SIGMA}[bg2];"
            f"[fg]crop={crop_w}:{src_h}:{crop_x}:0,"
            f"scale={w}:{h},setsar=1[fg2];"
            f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2[v0]"
        )
    else:  # fullscreen
        graph = (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1[v0]"
        )

    if ass_path:
        graph += f";[v0]ass={_escape_filter_path(ass_path)}[vout]"
        return graph, "vout"
    return graph, "v0"


# ---------------------------------------------------------------------------
# Per-creator presets (plain JSON under the data dir; no secrets)
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-.]{0,63}$")


def _check_name(value: str, what: str) -> str:
    name = str(value or "").strip()
    if not _NAME_RE.match(name):
        raise FacecamError(
            f"{what} must be 1-64 chars, letters/digits/space/_/-/. only, got {value!r}"
        )
    return name


class FacecamPresetStore:
    """Per-creator facecam presets in ``<data_dir>/facecam_presets.json``."""

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "facecam_presets.json"

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def list_presets(self, creator: str = "default") -> dict[str, dict]:
        """``{preset_name: layout_dict}`` for one creator (empty if none)."""
        creator = _check_name(creator, "creator")
        data = self._load()
        presets = data.get(creator)
        return dict(presets) if isinstance(presets, dict) else {}

    def list_all(self) -> dict[str, dict[str, dict]]:
        """``{creator: {preset_name: layout_dict}}`` for every creator."""
        data = self._load()
        return {
            creator: dict(presets)
            for creator, presets in data.items()
            if isinstance(presets, dict)
        }

    def save_preset(self, creator: str, name: str, layout: FacecamLayout) -> dict:
        creator = _check_name(creator, "creator")
        name = _check_name(name, "preset name")
        layout.validate()
        data = self._load()
        data.setdefault(creator, {})[name] = layout.to_dict()
        self._save(data)
        return layout.to_dict()

    def load_preset(self, creator: str, name: str) -> FacecamLayout:
        creator = _check_name(creator, "creator")
        name = _check_name(name, "preset name")
        presets = self.list_presets(creator)
        if name not in presets:
            raise FacecamError(f"no facecam preset {name!r} for creator {creator!r}")
        return FacecamLayout.from_dict(presets[name])

    def delete_preset(self, creator: str, name: str) -> bool:
        creator = _check_name(creator, "creator")
        name = _check_name(name, "preset name")
        data = self._load()
        presets = data.get(creator)
        if not isinstance(presets, dict) or name not in presets:
            return False
        del presets[name]
        if not presets:
            del data[creator]
        self._save(data)
        return True
