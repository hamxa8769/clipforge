"""SEO pack + Channel SEO API (v0.11.0, Worker B).

Endpoints (registered in ``clipforge_api.app`` — one include line, same
pattern as the growth router):

  GET  /clips/{candidate_id}/seo-pack
       Full SEO pack for one clip: title options, description, search
       tags, hashtags and one plain-language tip per platform
       (YouTube Shorts / TikTok / Instagram Reels / Facebook Reels / X),
       plus the whole pack as copy-all text. Built from the candidate
       (hook, transcript excerpt, duration) and its job (keyword from
       the job title); the worker's stored SEO variants win where they
       exist, so the pack never contradicts the Review page.
  POST /seo-pack/preview
       Same pack from a plain body ({keyword, hook, ...}) — no clip
       needed. Used by the Studio / tests and by any future “write the
       SEO before the clip exists” flow.
  GET  /channel-seo?channel_url=&topic=
       Channel SEO view for a channel link; without a link it uses the
       connected YouTube channel when there is one. Returns the cached
       analysis (24 h) when it is fresh, otherwise reads the channel
       live. No API key needed.
  POST /channel-seo/analyze
       Same, but always reads the channel live (ignores the cache).
  GET  /channel-seo/connected?topic=
       Channel SEO for the connected YouTube channel only — 404 with a
       plain message when no channel is connected.

Channel reads are public-page reads via yt-dlp (flat playlist, no
download). The saved YouTube Data API key (Trending's key, same store)
is used only to enrich views + real tags when it exists; without it
the public page numbers stand and the response says so.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from clipforge import channel_seo
from clipforge.auth.secrets import SecretStore
from clipforge.auth.token_store import TokenStore
from clipforge.config import settings
from clipforge.db import ClipCandidate, Job, init_db, session_factory
from clipforge.seo.pack import build_seo_pack
from clipforge.trending import load_youtube_api_key

router = APIRouter(tags=["seo"])

_engine = init_db()
_Session = session_factory()


def get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


def _data_dir() -> Path:
    # Same resolution as clipforge_api.app._data_dir(): the env override
    # wins so tests can point at a temp dir; otherwise the configured
    # data dir.
    return Path(os.environ.get("CLIPFORGE_DATA_DIR") or settings.data_dir)


def _secrets() -> SecretStore:
    return SecretStore(_data_dir())


# ------------------------------------------------------------------ SEO pack


class SeoPackPreview(BaseModel):
    """Body for POST /seo-pack/preview — plain words, no clip needed."""

    keyword: str = Field(min_length=1, description="Main word for the clip")
    hook: str = ""
    title_hint: str = ""
    duration_s: float = 30.0
    transcript: str = ""
    source_title: str = ""
    source_channel: str = ""


def _keyword_for_job(job: Job | None) -> str:
    title = (job.title or "") if job else ""
    return title.split(":")[0].strip() or "clip"


@router.get("/clips/{candidate_id}/seo-pack")
def get_clip_seo_pack(candidate_id: str, db: Session = Depends(get_db)) -> dict:
    """Full SEO pack for one clip (see module docstring)."""
    cand = db.get(ClipCandidate, candidate_id)
    if cand is None:
        raise HTTPException(404, f"clip candidate {candidate_id} not found")
    job = db.get(Job, cand.job_id) if cand.job_id else None
    from clipforge import automation  # local import: automation pulls the worker graph

    stored = automation.seo_for_candidate(db, cand)
    pack = build_seo_pack(
        _keyword_for_job(job),
        cand.hook_line or "",
        cand.hook_line or "",
        cand.duration_s or 30.0,
        transcript=cand.transcript_excerpt or "",
        source_title=cand.source_title or (job.title if job else "") or "",
        source_channel=cand.source_channel or "",
        attribution_text=cand.attribution_text,
        stored=stored or None,
    )
    pack["clip_candidate_id"] = cand.id
    pack["job_id"] = cand.job_id
    return pack


@router.post("/seo-pack/preview")
def preview_seo_pack(payload: SeoPackPreview) -> dict:
    """SEO pack from a plain body — no clip, no database."""
    try:
        return build_seo_pack(
            payload.keyword,
            payload.hook,
            payload.title_hint,
            payload.duration_s,
            transcript=payload.transcript,
            source_title=payload.source_title,
            source_channel=payload.source_channel,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ------------------------------------------------------------- Channel SEO


class ChannelSeoAnalyze(BaseModel):
    """Body for POST /channel-seo/analyze — always reads the channel live."""

    channel_url: str = Field(min_length=1, description="YouTube channel link or @handle")
    topic: str = ""
    limit: int = Field(default=channel_seo.DEFAULT_LIMIT, ge=5, le=50)


def _connected_youtube_channel() -> dict | None:
    """Connected YouTube channel (OAuth record), else None."""
    record = TokenStore(_secrets()).load("youtube")
    if record is None or record.is_expired() or not record.account_ref:
        return None
    return {
        "channel_id": record.account_ref,
        "channel_url": f"https://www.youtube.com/channel/{record.account_ref}",
        "account_name": record.account_name or "",
    }


def _analyze(channel_url: str, topic: str, limit: int, force_live: bool) -> dict:
    try:
        url = channel_seo.normalize_channel_url(channel_url)
    except channel_seo.ChannelSeoError as exc:
        raise HTTPException(400, str(exc)) from exc
    data_dir = _data_dir()
    if not force_live:
        # A fresh cache answers instantly; the channel id is in the URL
        # for /channel/ links, but @handles need a live read to resolve —
        # so the cache lookup below only applies when we know the id.
        channel_id = ""
        if "/channel/" in url:
            channel_id = url.rsplit("/channel/", 1)[1].strip("/")
        if channel_id:
            cached = channel_seo.load_cached(data_dir, channel_id)
            if cached is not None:
                return cached
    try:
        return channel_seo.analyze_live(
            url,
            topic=topic,
            limit=limit,
            data_dir=data_dir,
            youtube_api_key=load_youtube_api_key(_secrets()),
        )
    except channel_seo.ChannelSeoError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/channel-seo")
def get_channel_seo(channel_url: str = "", topic: str = "") -> dict:
    """Channel SEO view — cached when fresh, else a live read.

    Without ``channel_url`` the connected YouTube channel is used;
    with neither a link nor a connected channel the answer is a plain
    400 saying what to do.
    """
    url = (channel_url or "").strip()
    if not url:
        connected = _connected_youtube_channel()
        if connected is None:
            raise HTTPException(
                400,
                "Paste a YouTube channel link — or connect your YouTube "
                "channel on the Channels page and this view fills itself in.",
            )
        url = connected["channel_url"]
    return _analyze(url, topic, channel_seo.DEFAULT_LIMIT, force_live=False)


@router.post("/channel-seo/analyze")
def analyze_channel_seo(payload: ChannelSeoAnalyze) -> dict:
    """Channel SEO, always read live (ignores the cache)."""
    return _analyze(payload.channel_url, payload.topic, payload.limit, force_live=True)


@router.get("/channel-seo/connected")
def get_connected_channel_seo(topic: str = "") -> dict:
    """Channel SEO for the connected YouTube channel only."""
    connected = _connected_youtube_channel()
    if connected is None:
        raise HTTPException(
            404,
            "No YouTube channel is connected yet. Connect it on the "
            "Channels page — or paste any channel link on the Channel SEO "
            "page instead.",
        )
    analysis = _analyze(
        connected["channel_url"], topic, channel_seo.DEFAULT_LIMIT, force_live=False
    )
    analysis["connected_account_name"] = connected["account_name"]
    return analysis


__all__ = ["get_db", "router"]
