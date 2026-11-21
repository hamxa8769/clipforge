"""Live niche trending search + trending-to-agent loop (v0.6.0, Worker B).

Router: ``GET/POST /api/trending/*``. Registered in
``clipforge_api/app.py`` in one clearly-marked block (Worker A owns
that registration) — this module never imports ``app.py`` (circular
import: app.py registers this router).

Endpoints:

  GET  /api/trending/niches       (niche presets + labels)
  GET  /api/trending/credentials  (OAuth / API-key status, masked)
  POST /api/trending/api-key      (save/clear the YouTube Data API key)
  GET  /api/trending/search       (live YouTube search for a niche)
  POST /api/trending/match        (match a trending topic to YOUR OWN
                                   channel video on that topic)
  POST /api/trending/run          (start the agent on the matched own
                                   video, SEO tuned to the trend)
  GET  /api/trending/status       (agent run status, incl. trending job)
  GET  /api/trending/jobs/{id}    (job record: trending topic + each
                                   post's platform + provider video id)

Rights guardrail (applies to every string in this module): the agent
clips ONLY the user's own videos or videos they have rights / a licence
to use. Trending shows what topics are working — clips are made from
YOUR videos. A trending video URL is refused as a clip source
(422, plain language) by :func:`clipforge.trending.refuse_trending_source`
and by the agent's own validation.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from clipforge import agent as agent_mod
from clipforge.auth.secrets import SecretStore
from clipforge.auth.settings_store import SettingsStore
from clipforge.db import ClipCandidate, Job, Post, session_factory
from clipforge.niches import PRESET_NAMES, niche_label
from clipforge.trending import (
    TRENDING_NOTICE,
    TrendingError,
    credentials_status,
    is_trending_source,
    match_own_video,
    resolve_credentials,
    save_youtube_api_key,
    search_trending,
    youtube_api_key_view,
)

router = APIRouter(prefix="/api/trending", tags=["trending"])

_Session = session_factory()


def _data_dir() -> Path:
    # Same resolution as clipforge_api.app._data_dir(): the env override
    # wins so tests can point at a temp dir; otherwise the configured
    # data dir. This module must not import app.py (circular import).
    from clipforge.config import settings  # local import: cheap, no cycle

    return Path(os.environ.get("CLIPFORGE_DATA_DIR") or settings.data_dir)


def _secrets() -> SecretStore:
    return SecretStore(_data_dir())


def _settings_store() -> SettingsStore:
    return SettingsStore(_data_dir(), _secrets())


def get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


def _agent() -> agent_mod.Agent:
    """The one shared agent for this data dir (same as app.py's)."""
    return agent_mod.get_agent(_data_dir(), _Session, _settings_store())


# ------------------------------------------------------------------ schemas
class ApiKeyIn(BaseModel):
    """Save (or clear, with "") the YouTube Data API key."""

    api_key: str | None = None


class MatchIn(BaseModel):
    channel_url: str
    trending_video_id: str = ""
    trending_title: str = ""


class TrendingRunIn(BaseModel):
    """Start a trending agent run.

    ``trending_video_id`` / ``trending_title`` are the inspiration
    (topic only). ``own_source_url`` is the user's OWN video on that
    topic — it must NOT be the trending video itself (422). When
    ``own_source_type`` is "channel", the agent matches the channel's
    recent videos to the trending topic first (see /match).
    """

    niche: str = "general"
    trending_video_id: str = ""
    trending_title: str = ""
    own_source_url: str
    own_source_type: str = Field(default="video", pattern="^(video|channel)$")
    rights_confirmed: bool = False
    top_n: int = Field(default=3, ge=1, le=10)
    platforms: list[str] | None = None
    auto_post: bool = False
    scheduled_for: str | None = None
    # Test / verification only: when set, the run scores this text
    # instead of downloading a video (same demo path as the agent
    # config's transcript_text). The UI never sends it.
    demo_transcript_text: str | None = None


# ------------------------------------------------------------------ endpoints
@router.get("/niches")
def list_niches():
    """Niche presets (free text is also allowed — any niche works)."""
    return {
        "niches": list(PRESET_NAMES),
        "labels": {name: niche_label(name) for name in PRESET_NAMES},
        "notice": TRENDING_NOTICE,
    }


@router.get("/credentials")
def get_credentials():
    """YouTube credential status for trending search (masked, honest)."""
    return credentials_status(_secrets())


@router.post("/api-key")
def save_api_key(payload: ApiKeyIn):
    """Save (or clear, with "") a YouTube Data API key in the OS
    keyring. The response carries only the masked view — the key
    itself is never returned."""
    key = (payload.api_key or "").strip()
    save_youtube_api_key(_secrets(), key or None)
    view = youtube_api_key_view(_secrets())
    return {
        "ok": True,
        "configured": view["configured"],
        "last4": view["last4"],
        "masked": view["masked"],
        "message": (
            "YouTube Data API key saved."
            if view["configured"]
            else "YouTube Data API key cleared — trending search will use "
            "your connected YouTube channel, if any."
        ),
    }


@router.get("/search")
def trending_search(niche: str = "general", max_results: int = 10):
    """Live trending videos for a niche, ranked by trend score.

    422 (plain language) when no YouTube connection / API key exists
    or YouTube rejects the call — the page shows that as its
    needs-connection / error state. Never invented videos.
    """
    try:
        credentials = resolve_credentials(_secrets(), _settings_store())
    except TrendingError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        with httpx.Client(timeout=30.0) as client:
            videos = search_trending(
                niche,
                credentials=credentials,
                max_results=max(1, min(25, max_results)),
                client=client,
            )
    except TrendingError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "niche": niche,
        "results": [video.to_dict() for video in videos],
        "count": len(videos),
        "source": credentials.kind,
        "account": credentials.account_name,
        "notice": TRENDING_NOTICE,
    }


@router.post("/match")
def match_topic(payload: MatchIn):
    """Match a trending topic to the user's OWN channel video on it.

    Returns the best-matching own video (title overlap) + its match
    score. The user confirms it before any run starts. 422 when the
    channel cannot be read (plain language, e.g. yt-dlp missing).
    """
    try:
        matched = match_own_video(payload.channel_url, payload.trending_title)
    except TrendingError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {**matched, "trending_video_id": payload.trending_video_id, "notice": TRENDING_NOTICE}


@router.post("/run", status_code=202)
def start_trending_run(payload: TrendingRunIn):
    """Start the full loop: trending topic -> own video -> clips -> SEO
    (tuned to the trend) -> approval queue -> schedule / publish.

    422 when rights are not confirmed, when the source IS the trending
    video (guardrail), or when the channel cannot be matched. 409
    when the agent is already running. The run itself is async: poll
    GET /api/trending/status (or the pipeline) for progress.
    """
    if not payload.rights_confirmed:
        raise HTTPException(
            422,
            "Please confirm the source is yours or licensed first — "
            "the agent only works with videos you own or have the "
            "rights / licence to use.",
        )
    source_url = (payload.own_source_url or "").strip()
    if not source_url:
        raise HTTPException(422, "Give the agent your own source video or channel URL.")
    if is_trending_source(source_url, payload.trending_video_id):
        raise HTTPException(
            422,
            "That is the trending video itself — the agent cannot clip "
            "another creator's video. Pick YOUR OWN video on this topic "
            "as the source instead. " + TRENDING_NOTICE,
        )
    source_type = payload.own_source_type
    if source_type == "channel" and not payload.demo_transcript_text:
        # Match the channel's recent videos to the trending topic first;
        # the run then clips the best-matching OWN video.
        try:
            matched = match_own_video(source_url, payload.trending_title)
        except TrendingError as exc:
            raise HTTPException(422, str(exc)) from exc
        source_url = matched["own_video_url"]
        source_type = "video"
    config = agent_mod.AgentConfig(
        enabled=True,
        source_type=source_type,
        source_url=source_url,
        niche=payload.niche,
        auto_post=payload.auto_post,
        rights_confirmed=True,
        transcript_text=payload.demo_transcript_text,
        top_n=payload.top_n,
        platforms=list(payload.platforms) if payload.platforms else ["youtube"],
        trending_video_id=payload.trending_video_id,
        trending_title=payload.trending_title,
        trending_topic=payload.trending_title,
        scheduled_for=payload.scheduled_for,
    )
    agent = _agent()
    try:
        state = agent.start_trending(config)
    except agent_mod.AgentError as exc:
        message = str(exc)
        raise HTTPException(409 if "already running" in message else 422, message) from exc
    return {"ok": True, "state": state, "own_source_url": source_url}


@router.get("/status")
def trending_status():
    """Agent run status, including the trending video of the current /
    last run (topic only)."""
    return _agent().status()


@router.get("/jobs/{job_id}")
def trending_job(job_id: str, db: Session = Depends(get_db)):
    """One trending job's record: the trending topic it was inspired
    by, and every post's platform + provider video id (from the publish
    result) so the Growth view can monitor the posted clip."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    meta = dict(job.meta_json or {})
    candidate_ids = [c.id for c in db.query(ClipCandidate).filter_by(job_id=job.id).all()]
    posts = []
    if candidate_ids:
        for post in (
            db.query(Post)
            .filter(Post.clip_candidate_id.in_(candidate_ids))
            .order_by(Post.created_at)
            .all()
        ):
            result = post.publish_result_json or {}
            seo = post.seo_json or {}
            posts.append(
                {
                    "id": post.id,
                    "platform": post.platform,
                    "status": post.status,
                    "title": seo.get("title") or "",
                    "scheduled_for": post.scheduled_for.isoformat() if post.scheduled_for else None,
                    "published_at": post.published_at.isoformat()
                    if getattr(post, "published_at", None)
                    else None,
                    # The posted clip's video id on the platform (set on
                    # publish; null while the post is still queued).
                    "provider_post_id": result.get("provider_post_id"),
                    "video_id": result.get("provider_post_id"),
                }
            )
    return {
        "id": job.id,
        "title": job.title,
        "source": job.source_url or job.source_path or "",
        "source_type": job.source_type,
        "status": job.status,
        "own_content": job.own_content,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "trending_video_id": meta.get("trending_video_id") or "",
        "trending_title": meta.get("trending_title") or "",
        "trending_topic": meta.get("trending_topic") or "",
        "trending_url": meta.get("trending_url") or "",
        "demo": bool(meta.get("demo", False)),
        "posts": posts,
        "notice": TRENDING_NOTICE,
    }


__all__ = ["router"]
