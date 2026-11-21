"""Deeper AI clip detection endpoint (v0.8.0, Worker A).

Router: ``POST /detection/analyze``. Registered in
``clipforge_api/app.py`` by the coordinator — this module never
imports ``app.py`` (circular import: app.py registers this router).

Rights guardrail: detection scores transcript segments the caller
already has, for the user's own videos or Creative Commons /
licensed / public-domain videos only. It never downloads a video.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from clipforge import detection as detection_mod

router = APIRouter(prefix="/detection", tags=["detection"])


class SegmentIn(BaseModel):
    """One word-level transcript segment."""

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str = ""
    energy: float | None = Field(default=None, ge=0, le=1)
    speaker: str | None = None


class VisualCueIn(BaseModel):
    """One optional visual cue inside the video."""

    t: float = Field(ge=0)
    scene_change: bool = False
    faces: int = Field(default=0, ge=0)


class AnalyzeIn(BaseModel):
    """Detect clip candidates from transcript segments."""

    segments: list[SegmentIn]
    duration: float | None = Field(default=None, ge=0)
    # The web client sends a single niche string ("gaming"); accept a
    # string or a list and normalise to keywords.
    niche: str | list[str] | None = None
    visual: list[VisualCueIn] | None = None
    top_n: int = Field(default=10, ge=1, le=12)

    @field_validator("niche", mode="before")
    @classmethod
    def _niche_to_keywords(cls, value: object) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return [value] if value.strip() else None
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        return None


@router.post("/analyze")
def analyze(payload: AnalyzeIn):
    """Score transcript segments and return ranked clip candidates.

    422 (plain language) when there is too little transcript to cut
    even one 15 s clip. Never a traceback: bad input is a 422, and
    detection itself is pure functions over the given segments.
    """
    if not payload.segments:
        raise HTTPException(422, "Give at least one transcript segment to analyse.")
    try:
        candidates = detection_mod.analyze(
            segments=[s.model_dump() for s in payload.segments],
            duration=payload.duration,
            niche_keywords=payload.niche,
            visual_cues=[v.model_dump() for v in payload.visual] if payload.visual else None,
            top_n=payload.top_n,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, f"Could not analyse those segments: {exc}") from exc
    if not candidates:
        raise HTTPException(
            422,
            "Too little transcript to cut a clip — a clip needs at least 15 seconds of transcript.",
        )
    return {"candidates": candidates, "count": len(candidates)}


__all__ = ["router"]
