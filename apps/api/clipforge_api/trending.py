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

Live clipping (v0.7.0, Worker C) — same router, ``/api/trending/live``:

  GET  /api/trending/live              (live streams in a niche, right
                                        now; ``licensed_only=true``
                                        keeps Creative Commons only)
  GET  /api/trending/live/mine         (the connected channel's own
                                        live broadcasts)
  POST /api/trending/live/capture      (record a bounded 60–300s
                                        segment of an own / CC live
                                        stream, then run the normal
                                        pipeline on it — transcribe ->
                                        score -> clip -> SEO -> approval
                                        queue; 202, poll GET /jobs/{id})
  POST /api/trending/live/{job_id}/cancel (stop a running live capture)

Rights guardrail (applies to every string in this module): the agent
clips ONLY the user's own videos or videos they have rights / a licence
to use. Trending shows what topics are working — clips are made from
YOUR videos. A trending video URL is refused as a clip source
(422, plain language) by :func:`clipforge.trending.refuse_trending_source`
and by the agent's own validation. Live clipping extends the same rule
to live streams: only the user's OWN live streams and Creative Commons
licensed live streams may be captured — the licence is re-verified
against the YouTube API at capture time, never trusted from the client.

Creative Commons discovery (v0.7.0, Worker A):

  GET  /api/trending/search?licensed_only=true — trending search
       restricted to CC-licensed videos (verified per video, see
       ``clipforge.trending.search_trending``). Default false.
  GET  /api/videos/license?url=<youtube-url> — licence check for one
       video: {video_id, title, channel, channel_id, license,
       license_url, can_clip, reason}. can_clip is true only for a
       Creative Commons video or the user's own video.
  POST /api/trending/run — the licence guardrail: when YouTube
       credentials are available, a real (non-demo) video source is
       verified live; a standard-licence third-party video is refused
       (422), an unverifiable video is refused honestly. When no
       credentials exist the server cannot verify, so the run proceeds
       on the user's rights confirmation (v0.6.0 behaviour) — it never
       invents a licence verdict. Demo-transcript runs skip the check
       (they download nothing).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from clipforge import agent as agent_mod
from clipforge.auth.secrets import SecretStore
from clipforge.auth.settings_store import SettingsStore
from clipforge.db import ClipCandidate, Job, JobStatus, Post, WorkerRun, session_factory
from clipforge.live import (
    LIVE_DEFAULT_DURATION_S,
    LIVE_MAX_DURATION_S,
    LIVE_MIN_DURATION_S,
    LIVE_NOTICE,
    LiveError,
    canonical_source_license,
    clear_live_cancel,
    list_own_live,
    request_live_cancel,
    resolve_own_channel_id,
    search_live,
    verify_live_video,
)
from clipforge.niches import PRESET_NAMES, niche_label
from clipforge.trending import (
    TRENDING_NOTICE,
    TrendingError,
    check_video_license,
    credentials_status,
    extract_youtube_video_id,
    is_trending_source,
    match_own_video,
    own_channel_id,
    resolve_credentials,
    save_youtube_api_key,
    search_trending,
    youtube_api_key_view,
)
from clipforge.worker import run_job as worker_run_job

router = APIRouter(prefix="/api/trending", tags=["trending"])

# Licence-check router (v0.7.0, Worker A): GET /api/videos/license.
# A separate router so the path is /api/videos/*, not /api/trending/*.
videos_router = APIRouter(prefix="/api/videos", tags=["videos"])

_Session = session_factory()

# Live capture jobs run in background threads (a capture records for
# 60–300s, so the POST returns 202 and the UI polls GET /jobs/{id}).
# The dict only holds the threads so cancel can tell a running capture
# from a not-yet-started one; the job row is the source of truth.
_LIVE_THREADS: dict[str, threading.Thread] = {}


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


class LiveCaptureIn(BaseModel):
    """Start a live capture.

    ``video_url`` (or ``video_id``) is the live stream to record. The
    licence is NOT trusted from the client: the endpoint re-verifies
    it against the YouTube API, and only the user's own live stream
    or a Creative Commons licensed live stream is captured.
    ``duration_s`` is the bounded recording length (60–300s).
    """

    video_url: str | None = None
    video_id: str | None = None
    duration_s: int = Field(
        default=LIVE_DEFAULT_DURATION_S, ge=LIVE_MIN_DURATION_S, le=LIVE_MAX_DURATION_S
    )
    niche: str = "general"
    top_n: int = Field(default=3, ge=1, le=10)
    rights_confirmed: bool = False


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
def trending_search(niche: str = "general", max_results: int = 10, licensed_only: bool = False):
    """Live trending videos for a niche, ranked by trend score.

    ``licensed_only=true`` restricts the search to Creative Commons
    licensed videos: ``videoLicense=creativeCommon`` on search.list
    plus a per-video ``status.license`` verification (anything that
    fails verification is dropped). Default false: the v0.6.0 search
    over all trending videos.

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
                licensed_only=licensed_only,
                client=client,
            )
    except TrendingError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "niche": niche,
        "licensed_only": licensed_only,
        "results": [video.to_dict() for video in videos],
        "count": len(videos),
        "source": credentials.kind,
        "account": credentials.account_name,
        "notice": TRENDING_NOTICE,
    }


@videos_router.get("/license")
def check_license(url: str):
    """Licence check for one YouTube video (v0.7.0, Worker A).

    Returns ``{video_id, title, channel, channel_id, license,
    license_url, can_clip, reason}``. ``can_clip`` is true only for a
    Creative Commons licensed video or the user's own video (matched
    against the connected YouTube channel id when one is connected).
    A standard-licence video returns ``can_clip=false`` with the
    plain-language reason — never a faked licence result.

    422 (plain language) when no YouTube connection / API key exists,
    the URL is not a YouTube video URL, YouTube has no such video, or
    the API call fails.
    """
    try:
        credentials = resolve_credentials(_secrets(), _settings_store())
    except TrendingError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        # No client passed: check_video_license extracts + validates
        # the video id BEFORE opening any HTTP client, so a bad URL
        # fails fast with a plain 422 instead of a client error.
        return check_video_license(
            url,
            credentials=credentials,
            own_channel_id=own_channel_id(_secrets()),
        )
    except TrendingError as exc:
        raise HTTPException(422, str(exc)) from exc


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
    video (guardrail), when the source is a standard-licence
    third-party video or its licence cannot be verified (v0.7.0
    licence guardrail, when YouTube credentials exist), or when the
    channel cannot be matched. 409
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
    if source_type == "video" and not payload.demo_transcript_text:
        # Licence guardrail (v0.7.0, Worker A): verify the source
        # video's licence live, when YouTube credentials exist. A
        # standard-licence third-party video is refused (422, plain
        # language); an unverifiable video is refused honestly. When
        # no credentials exist the server cannot verify, so the run
        # proceeds on the user's rights confirmation (v0.6.0
        # behaviour) — it never invents a licence verdict. The one
        # exception is a standard-licence video whose ownership
        # cannot be established at all (API-key-only setup, no
        # connected channel): the rights attestation stands, because
        # refusing it would also refuse the user's own videos.
        # Demo-transcript runs skip this (they download nothing) and
        # channel-matched runs skip it (the match is an own video by
        # construction).
        try:
            credentials = resolve_credentials(_secrets(), _settings_store())
        except TrendingError:
            credentials = None
        if credentials is not None:
            own_id = own_channel_id(_secrets())
            try:
                # No client passed: check_video_license validates the
                # video id before opening any HTTP client.
                licence = check_video_license(
                    source_url,
                    credentials=credentials,
                    own_channel_id=own_id,
                )
            except TrendingError as exc:
                raise HTTPException(422, str(exc)) from exc
            if not licence["can_clip"]:
                if licence["license"] == "youtube" and not own_id:
                    pass  # ownership unknowable; rights attestation stands
                else:
                    raise HTTPException(422, licence["reason"])
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


# ------------------------------------------------------------------ live (v0.7.0, Worker C)
def _run_live_job_thread(job_id: str) -> None:
    """Run one live job through the worker (own session, own thread)."""
    session = _Session()
    try:
        worker_run_job(session, job_id, data_dir=_data_dir())
    finally:
        session.close()


@router.get("/live")
def list_live(niche: str = "general", licensed_only: bool = False, max_results: int = 10):
    """Live streams in a niche, right now.

    ``search.list`` with ``eventType=live`` (+ ``videoLicense=
    creativeCommon`` when ``licensed_only``), licence verified per
    video via ``videos.list``. Each result carries ``viewers``
    (concurrent), ``started_at``, ``licence`` and ``can_clip`` — True
    only for the user's own live streams and Creative Commons
    licensed ones. 422 (plain language) when no YouTube connection /
    API key exists or YouTube rejects the call. Never invented streams.
    """
    try:
        credentials = resolve_credentials(_secrets(), _settings_store())
    except TrendingError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        with httpx.Client(timeout=30.0) as client:
            own_id = resolve_own_channel_id(_secrets(), credentials=credentials, client=client)
            videos = search_live(
                niche,
                credentials=credentials,
                licensed_only=licensed_only,
                own_channel_id=own_id,
                max_results=max(1, min(25, max_results)),
                client=client,
            )
    except LiveError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "niche": niche,
        "licensed_only": licensed_only,
        "results": [video.to_dict() for video in videos],
        "count": len(videos),
        "source": credentials.kind,
        "account": credentials.account_name,
        "notice": LIVE_NOTICE,
    }


@router.get("/live/mine")
def list_my_live():
    """The connected channel's current live broadcasts.

    ``liveBroadcasts.list`` (``mine=true``) when the OAuth token
    allows it, else a channel search with ``eventType=live``. Every
    result is the user's own: ``is_own`` and ``can_clip`` are True.
    422 (plain language) when no channel is connected or YouTube
    refuses the call.
    """
    try:
        credentials = resolve_credentials(_secrets(), _settings_store())
    except TrendingError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        with httpx.Client(timeout=30.0) as client:
            own_id = resolve_own_channel_id(_secrets(), credentials=credentials, client=client)
            videos = list_own_live(credentials=credentials, own_channel_id=own_id, client=client)
    except LiveError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "results": [video.to_dict() for video in videos],
        "count": len(videos),
        "own_channel_id": own_id,
        "notice": LIVE_NOTICE,
    }


@router.post("/live/capture", status_code=202)
def start_live_capture(payload: LiveCaptureIn, db: Session = Depends(get_db)):
    """Record a bounded segment of a live stream, then clip it.

    The stream's licence is re-verified against the YouTube API
    (never trusted from the request): only the user's own live stream
    or a Creative Commons licensed live stream is captured — a
    standard-licence third-party stream is refused with 422 and a
    plain-language reason. The capture records ``duration_s`` seconds
    (60–300s) with yt-dlp, then the job runs the NORMAL worker
    pipeline on the recording (transcribe -> score -> clip -> SEO ->
    approval queue). 202 with the job id; poll GET /jobs/{id} for
    progress, and POST /api/trending/live/{job_id}/cancel to stop it.
    """
    if not payload.rights_confirmed:
        raise HTTPException(
            422,
            "Please confirm the source is yours or licensed first — "
            "live clipping only works on your own live streams and "
            "Creative Commons licensed live streams.",
        )
    video_id = extract_youtube_video_id(payload.video_url) or (payload.video_id or "").strip()
    if not video_id or len(video_id) != 11:
        raise HTTPException(
            422, "Give a YouTube live video URL (youtube.com/watch?v=… or youtu.be/…)."
        )
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        credentials = resolve_credentials(_secrets(), _settings_store())
    except TrendingError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        with httpx.Client(timeout=30.0) as client:
            own_id = resolve_own_channel_id(_secrets(), credentials=credentials, client=client)
            verified = verify_live_video(
                video_id, credentials=credentials, own_channel_id=own_id, client=client
            )
    except LiveError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not verified.can_clip:
        raise HTTPException(422, verified.clip_reason)
    job = Job(
        source_type="live",
        source_url=url,
        title=verified.title or f"Live capture — {payload.niche}",
        own_content=True,
        status=JobStatus.PENDING.value,
        # Licence record (Worker A vocabulary): feeds the worker's
        # thumbnail + attribution step and the publish guardrail.
        source_license=canonical_source_license(verified.licence, verified.is_own),
        source_title=verified.title or None,
        source_channel=verified.channel_title or None,
        meta_json={
            "live": True,
            "live_video_id": video_id,
            "live_title": verified.title,
            "live_channel": verified.channel_title,
            "live_licence": verified.licence,
            "live_is_own": verified.is_own,
            "live_duration_s": payload.duration_s,
            "niche": payload.niche,
            "top_n": payload.top_n,
            "caption_style": "bold-pop",
        },
    )
    db.add(job)
    db.commit()
    thread = threading.Thread(
        target=_run_live_job_thread,
        args=(job.id,),
        daemon=True,
        name=f"clipforge-live-{job.id}",
    )
    _LIVE_THREADS[job.id] = thread
    thread.start()
    return {
        "ok": True,
        "job_id": job.id,
        "status": JobStatus.PENDING.value,
        "video_id": video_id,
        "title": verified.title,
        "licence": verified.licence,
        "is_own": verified.is_own,
        "duration_s": payload.duration_s,
        "message": (
            f"Recording {payload.duration_s} seconds of the live stream — "
            f"poll GET /jobs/{job.id} for progress. The recorded segment "
            "then goes through the normal clip -> SEO -> approval flow."
        ),
        "notice": LIVE_NOTICE,
    }


@router.post("/live/{job_id}/cancel")
def cancel_live_capture(job_id: str, db: Session = Depends(get_db)):
    """Stop a live capture.

    A running capture stops within a few seconds (the partial
    recording is deleted, the job goes ``cancelled``) — poll
    GET /jobs/{id} for the final status. A job that has not started
    yet is cancelled outright. Finished jobs cannot be cancelled.
    404 when the job does not exist, 409 when it is already finished
    or is not a live job.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    if job.source_type != "live":
        raise HTTPException(
            409,
            "Only live capture jobs can be cancelled here — use "
            f"POST /jobs/{job_id}/cancel for other jobs.",
        )
    if job.status in {
        JobStatus.REVIEW.value,
        JobStatus.READY.value,
        JobStatus.FAILED.value,
        JobStatus.BLOCKED.value,
        JobStatus.CANCELLED.value,
    }:
        raise HTTPException(409, f"job is already '{job.status}' — cannot cancel it")
    thread = _LIVE_THREADS.get(job.id)
    if thread is not None and thread.is_alive():
        request_live_cancel(job.id)
        return {
            "ok": True,
            "job_id": job.id,
            "status": job.status,
            "message": (
                "Cancel requested — the recording will stop within a few "
                f"seconds and no clip will be made from it. Poll "
                f"GET /jobs/{job.id} for the final status."
            ),
        }
    job.status = JobStatus.CANCELLED.value
    job.error = "Live capture cancelled before recording started."
    db.query(WorkerRun).filter_by(job_id=job.id, status="running").update(
        {"status": "cancelled", "outcome": "cancelled by user"}
    )
    db.commit()
    clear_live_cancel(job.id)
    return {
        "ok": True,
        "job_id": job.id,
        "status": JobStatus.CANCELLED.value,
        "message": "Live capture cancelled.",
    }


__all__ = ["router", "videos_router"]
