"""Monetization + pre-publish scan router (v0.8.0, Worker C).

Two routers in this file; the coordinator includes both in
``clipforge_api/app.py`` (this module never imports ``app.py``):

  ``router`` (prefix ``/monetization``):
    POST /monetization/affiliate  (attach an affiliate link to a clip)
    POST /monetization/ab         (create A/B title/thumbnail variants)
    GET  /monetization/ab/{clip_id} (variants with views/CTR where measured)
    GET  /monetization/revenue    (per-platform earnings — honest, never invented)

  ``publish_router`` (prefix ``/publish``):
    POST /publish/scan            (metadata copyright scan before publish)

Honesty rules (enforced in ``clipforge.monetization`` / ``clipforge.copyright_scan``,
not just in the UI): revenue ``earnings`` are null unless a platform API
actually reported them; A/B ``views`` are 0 / "not measured yet" until a
recorded outcome or a Growth snapshot exists; ``ctr`` is always null —
ClipForge reads views/likes/comments only, never impressions. The
copyright scan is a metadata check only, never a Content ID check.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from clipforge.auth.secrets import SecretStore
from clipforge.auth.token_store import TokenStore
from clipforge.config import settings
from clipforge.copyright_scan import scan_clip
from clipforge.db import ClipCandidate, Job, init_db, session_factory
from clipforge.monetization import (
    MonetizationError,
    ab_results_for_clip,
    attach_affiliate_link,
    create_ab_variants,
    revenue_rows,
)

router = APIRouter(prefix="/monetization", tags=["monetization"])
publish_router = APIRouter(prefix="/publish", tags=["publish"])

_engine = init_db()
_Session = session_factory()


def _data_dir() -> Path:
    # Same resolution as clipforge_api.app._data_dir(): the env override
    # wins so tests can point at a temp dir; otherwise the configured
    # data dir. This module must not import app.py (circular import).
    return Path(os.environ.get("CLIPFORGE_DATA_DIR") or settings.data_dir)


def _secrets() -> SecretStore:
    return SecretStore(_data_dir())


def get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


def _get_clip_or_404(db: Session, clip_id: str) -> ClipCandidate:
    clip = db.get(ClipCandidate, clip_id)
    if clip is None:
        raise HTTPException(404, f"clip {clip_id} not found")
    return clip


# ------------------------------------------------------------------ schemas
class AffiliateIn(BaseModel):
    clip_id: str
    url: str


class AbVariantIn(BaseModel):
    title: str
    thumbnail: str | None = None
    platform: str | None = None
    is_control: bool = False


class AbCreateIn(BaseModel):
    clip_id: str
    variants: list[AbVariantIn] = Field(default_factory=list)


class PublishScanIn(BaseModel):
    clip_id: str


# ------------------------------------------------------------------ affiliate
@router.post("/affiliate")
def add_affiliate(payload: AffiliateIn, db: Session = Depends(get_db)):
    """Attach an affiliate link to a clip.

    The link is stored on the clip's job (``meta_json["affiliate_links"]``)
    and appended (UTM-tagged, per platform) to the clip's stored SEO
    descriptions and to any not-yet-published posts for the clip, so it
    goes out with the next publish. 422 for a non-http(s) URL, 404 for
    an unknown clip.
    """
    _get_clip_or_404(db, payload.clip_id)
    try:
        result = attach_affiliate_link(db, payload.clip_id, payload.url)
    except (MonetizationError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, **result}


# ------------------------------------------------------------------ A/B
@router.post("/ab")
def create_ab(payload: AbCreateIn, db: Session = Depends(get_db)):
    """Create A/B title (+optional thumbnail) variants for a clip.

    Titles are stored with the existing A/B store; thumbnails are
    stored in the ``ab_thumbnails.json`` sidecar (see
    ``clipforge.monetization``). 422 for an empty variant list, an
    empty title, or a bad thumbnail URL; 404 for an unknown clip.
    """
    _get_clip_or_404(db, payload.clip_id)
    try:
        variants = create_ab_variants(
            db,
            payload.clip_id,
            [v.model_dump() for v in payload.variants],
            _data_dir(),
        )
    except (MonetizationError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "clip_id": payload.clip_id, "variants": variants}


@router.get("/ab/{clip_id}")
def get_ab(clip_id: str, db: Session = Depends(get_db)):
    """A/B results for a clip: variants with views/CTR where measured.

    ``views`` is 0 and ``measured`` is false until a recorded outcome
    or a Growth snapshot exists ("not measured yet", never invented).
    ``ctr`` is null: ClipForge has no impressions source, so CTR
    cannot be computed. 404 for an unknown clip.
    """
    _get_clip_or_404(db, clip_id)
    return {
        "clip_id": clip_id,
        "variants": ab_results_for_clip(db, clip_id, _data_dir()),
    }


# ------------------------------------------------------------------ revenue
@router.get("/revenue")
def get_revenue(db: Session = Depends(get_db)):
    """Revenue dashboard: one row per platform.

    ``views`` are measured Growth-stats totals (0 when nothing was
    measured). ``earnings`` is real only for YouTube when the channel
    is connected AND the YouTube Analytics call succeeds; everywhere
    else it is null with ``basis: "not reported by this platform's
    API"``. Nothing is ever invented.
    """
    youtube_token: str | None = None
    try:
        record = TokenStore(_secrets()).load("youtube")
    except Exception:
        record = None
    if record is not None and record.access_token and not record.is_expired():
        youtube_token = record.access_token
    return {
        "platforms": revenue_rows(
            db,
            data_dir=_data_dir(),
            youtube_access_token=youtube_token,
        )
    }


# ------------------------------------------------------------------ publish scan
@publish_router.post("/scan")
def scan_for_publish(payload: PublishScanIn, db: Session = Depends(get_db)):
    """Copyright scan before publishing a clip.

    Checks available metadata only: source licence (own/CC/licensed
    vs unknown), audio-track flags recorded on the job, duration vs
    platform rules, and whether the clip file exists. Returns
    ``{clip_id, flags, disclaimer}``. The disclaimer states the scan
    is a metadata check only — NOT a guarantee of copyright safety
    and NOT a Content ID check. 404 for an unknown clip.
    """
    clip = _get_clip_or_404(db, payload.clip_id)
    job = db.get(Job, clip.job_id) if clip.job_id else None
    return scan_clip(clip, job)


__all__ = ["publish_router", "router"]
