"""Facecam Studio endpoints (v0.10.0).

Router: ``GET /facecam/styles``, ``GET/POST/DELETE /facecam/presets``,
``POST /facecam/render``, ``GET /facecam/media/{filename}``.
Registered in ``clipforge_api/app.py`` by the coordinator — this
module never imports ``app.py`` (circular import: app.py registers
this router).

``POST /facecam/render`` is the Studio's one-shot render: it renders
one clip range from a source the user already has (a local file under
the data dir, a direct video URL, or a YouTube URL with the same
rights declaration ``POST /jobs`` takes) with a Facecam Studio layout
and an animated caption style, through the same
:func:`clipforge.render.render_clip` the job queue uses. The full
pipeline path is ``POST /jobs`` with ``facecam_layout`` in the body —
those jobs run through the job queue and render every candidate with
the layout.

Rights guardrail: the same advisory rule as ``POST /jobs`` (v0.9.1) —
any pasted link proceeds, the UI shows the non-blocking rights
notice, and the user is responsible for the rights to what they post.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from clipforge.captions import ANIMATED_STYLE_DEFS, STYLES
from clipforge.facecam import (
    LAYOUT_MODES,
    MODE_LABELS,
    PIP_POSITIONS,
    FacecamError,
    FacecamLayout,
    FacecamPresetStore,
)
from clipforge.highlights import Candidate
from clipforge.ingest import MAX_SOURCE_BYTES, ingest_youtube
from clipforge.notifications import notify_best_effort
from clipforge.render import RenderError, render_clip
from clipforge.transcribe import Word

router = APIRouter(prefix="/facecam", tags=["facecam"])

MAX_RENDER_SECONDS = 300.0  # one-shot Studio renders stay interactive
_DIRECT_SUFFIXES = {".mp4", ".mov"}
_MEDIA_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".ass": "text/plain"}


def _data_dir() -> Path:
    # Same resolution as clipforge_api.app._data_dir(): the env override
    # wins so tests can point at a temp dir; otherwise the configured
    # data dir. This module must not import app.py (circular import).
    from clipforge.config import settings  # local import: cheap, no cycle

    return Path(os.environ.get("CLIPFORGE_DATA_DIR") or settings.data_dir)


def _store() -> FacecamPresetStore:
    return FacecamPresetStore(_data_dir())


# ---------------------------------------------------------------------------
# Styles + layout modes
# ---------------------------------------------------------------------------


@router.get("/styles")
def get_styles():
    """Caption styles (incl. the 6 animated Facecam styles) + layout modes."""
    caption_styles = [
        {
            "id": name,
            "label": (
                ANIMATED_STYLE_DEFS[name]["label"]
                if name in ANIMATED_STYLE_DEFS
                else name.replace("-", " ").title()
            ),
            "animated": name in ANIMATED_STYLE_DEFS,
            "fontsize": STYLES[name].fontsize,
        }
        for name in sorted(STYLES)
    ]
    return {
        "caption_styles": caption_styles,
        "layout_modes": [
            {"id": mode, "label": MODE_LABELS[mode]} for mode in LAYOUT_MODES
        ],
        "pip_positions": list(PIP_POSITIONS),
    }


# ---------------------------------------------------------------------------
# Per-creator presets
# ---------------------------------------------------------------------------


class PresetIn(BaseModel):
    creator: str = "default"
    name: str
    layout: dict = Field(default_factory=dict)


@router.get("/presets")
def list_presets(creator: str = "default", all: bool = False):
    """Presets for one creator (or every creator with ``all=true``)."""
    try:
        if all:
            return {"presets": _store().list_all()}
        return {"creator": creator, "presets": _store().list_presets(creator)}
    except FacecamError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/presets", status_code=201)
def save_preset(payload: PresetIn):
    """Save (or overwrite) one preset for a creator."""
    try:
        layout = FacecamLayout.from_dict(payload.layout)
        saved = _store().save_preset(payload.creator, payload.name, layout)
    except FacecamError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"creator": payload.creator, "name": payload.name, "layout": saved}


@router.delete("/presets/{creator}/{name}")
def delete_preset(creator: str, name: str):
    """Delete one preset; 404 when it does not exist."""
    try:
        deleted = _store().delete_preset(creator, name)
    except FacecamError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not deleted:
        raise HTTPException(404, f"no facecam preset {name!r} for creator {creator!r}")
    return {"deleted": True, "creator": creator, "name": name}


# ---------------------------------------------------------------------------
# One-shot Studio render
# ---------------------------------------------------------------------------


class RenderWordIn(BaseModel):
    text: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)


class FacecamRenderIn(BaseModel):
    source_path: str | None = None
    source_url: str | None = None
    own_content_declaration: bool = False
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    layout: dict = Field(default_factory=dict)
    caption_style: str = "bold-pop"
    words: list[RenderWordIn] = Field(default_factory=list)


def _resolve_local_source(source_path: str) -> Path:
    root = _data_dir().resolve()
    given = Path(source_path)
    target = (root / given).resolve() if not given.is_absolute() else given.resolve()
    if not target.is_relative_to(root):
        raise HTTPException(422, "source_path must be inside the ClipForge data dir")
    if target.suffix.lower() not in _DIRECT_SUFFIXES or not target.is_file():
        raise HTTPException(422, "source_path must be an existing .mp4 / .mov file")
    return target


def _is_youtube_url(url: str) -> bool:
    lowered = url.lower()
    return "youtube.com/watch" in lowered or "youtu.be/" in lowered


def _fetch_remote_video(url: str) -> Path:
    """Stream a direct video URL into data/uploads (10 GiB cap)."""
    name = Path(url.split("?")[0]).name or "remote.mp4"
    if Path(name).suffix.lower() not in _DIRECT_SUFFIXES:
        raise HTTPException(422, "source_url must point at a .mp4 / .mov file or a YouTube URL")
    uploads = _data_dir() / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    stored = uploads / f"facecam_{uuid.uuid4().hex[:12]}_{name}"
    written = 0
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(stored, "wb") as out:
                for chunk in resp.iter_bytes(1024 * 1024):
                    written += len(chunk)
                    if written > MAX_SOURCE_BYTES:
                        raise HTTPException(422, "file exceeds the 10 GiB source cap")
                    out.write(chunk)
    except HTTPException:
        stored.unlink(missing_ok=True)
        raise
    except Exception as exc:
        stored.unlink(missing_ok=True)
        raise HTTPException(422, f"could not download source_url: {exc}") from exc
    return stored


@router.post("/render", status_code=201)
def render_facecam(payload: FacecamRenderIn):
    """Render one facecam-layout clip (synchronous, <= 5 min of video).

    Returns the rendered file, its ASS captions, and the ffprobe
    verification — the same verification :func:`clipforge.render.render_clip`
    runs for queued jobs (1080x1920, duration within 1.5 s).
    """
    if bool(payload.source_path) == bool(payload.source_url):
        raise HTTPException(422, "pass exactly one of source_path or source_url")
    duration = payload.end_s - payload.start_s
    if duration <= 0 or duration > MAX_RENDER_SECONDS:
        raise HTTPException(
            422, f"clip must be 0-300 s long, got {duration:.1f} s"
        )
    if payload.caption_style not in STYLES:
        raise HTTPException(
            422,
            f"unknown caption style {payload.caption_style!r}; "
            f"choose from {sorted(STYLES)}",
        )
    try:
        layout = FacecamLayout.from_dict(payload.layout)
    except FacecamError as exc:
        raise HTTPException(422, str(exc)) from exc

    if payload.source_path:
        source = _resolve_local_source(payload.source_path)
    elif _is_youtube_url(payload.source_url or ""):
        try:
            info = ingest_youtube(
                payload.source_url or "",
                own_content=payload.own_content_declaration,
                out_dir=_data_dir() / "uploads",
            )
        except Exception as exc:
            raise HTTPException(422, f"could not download YouTube source: {exc}") from exc
        source = Path(info.path)
    else:
        source = _fetch_remote_video(payload.source_url or "")

    words = [
        Word(text=w.text, start=float(w.start), end=float(w.end))
        for w in payload.words
        if w.end > payload.start_s and w.start < payload.end_s
    ]
    cand = Candidate(
        start_word_index=0,
        end_word_index=0,
        start_s=payload.start_s,
        end_s=payload.end_s,
        score=0,
        hook=5,
        hook_line="",
        reason="Facecam Studio one-shot render",
    )
    out_dir = _data_dir() / "clips" / "facecam"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"facecam_{uuid.uuid4().hex[:12]}"
    out_path = out_dir / f"{stem}.mp4"
    try:
        result = render_clip(
            source,
            cand,
            out_path,
            style=payload.caption_style,
            words=words or None,
            facecam=layout,
        )
    except (RenderError, ValueError) as exc:
        raise HTTPException(422, f"facecam render failed: {exc}") from exc
    ass_path = out_path.with_suffix(".ass")
    notify_best_effort(
        "success",
        "Facecam clip rendered",
        f"Rendered {duration:.0f} s with the {layout.mode} layout "
        f"({payload.caption_style} captions).",
    )
    return {
        "file": str(result.path),
        "filename": out_path.name,
        "media_url": f"/facecam/media/{out_path.name}",
        "ass_file": str(ass_path) if ass_path.exists() else None,
        "ass_media_url": (
            f"/facecam/media/{ass_path.name}" if ass_path.exists() else None
        ),
        "layout": layout.to_dict(),
        "caption_style": payload.caption_style,
        "render": result.to_dict(),
    }


@router.get("/media/{filename}")
def facecam_media(filename: str):
    """Serve a rendered facecam clip (.mp4/.mov) or its captions (.ass)."""
    name = Path(filename).name
    if Path(name).suffix.lower() not in _MEDIA_TYPES or name != filename:
        raise HTTPException(404, "file not found")
    path = (_data_dir() / "clips" / "facecam" / name).resolve()
    root = (_data_dir() / "clips" / "facecam").resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(path, media_type=_MEDIA_TYPES[path.suffix.lower()])
