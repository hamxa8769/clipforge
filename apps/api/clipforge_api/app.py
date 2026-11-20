"""ClipForge API v0.1.0 — FastAPI app.

Endpoints (all dry-run by default; nothing publishes live without an
approved Post record, explicit credentials, and ``dry_run=false``):

  GET  /health
  POST /jobs                         (own_content declaration required)
  GET  /jobs | GET /jobs/{id} | GET /jobs/{id}/candidates
  POST /jobs/{id}/candidates/{n}/approve | reject | trim
  POST /posts                        (from an APPROVED clip only)
  GET  /posts?status=                (approval queue)
  POST /posts/{id}/approve | POST /posts/{id}/publish (dry-run default)
  GET  /posts/{id}
  GET  /accounts | POST /accounts    (token stored as env-var NAME only)
  GET  /monetization                 (tracker; ?platform=&subs=&...)
  GET  /analytics | GET /analytics/{account_id}
  POST /scheduler/run                (run due posts; dry-run default)

Run:  PYTHONPATH=src uvicorn clipforge_api.app:app --port 8000
      (from apps/api; see requirements.txt)
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Make `src/clipforge/...` importable whether uvicorn is launched from
# apps/api, from the repo root, or from anywhere else.
for _candidate in (
    Path(__file__).resolve().parents[2] / "src",
    Path(__file__).resolve().parents[3] / "src",
    Path.cwd() / "src",
):
    if (_candidate / "clipforge").is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from clipforge import __version__ as _VERSION  # noqa: F401  (package version)
from clipforge.analytics import ingest_all, retention_signals
from clipforge.config import settings
from clipforge.db import (
    AnalyticsSnapshot,
    ClipCandidate,
    Job,
    JobStatus,
    MonetizationProgress,
    Post,
    PostStatus,
    SocialAccount,
    init_db,
    session_factory,
)
from clipforge.monetization import progress as monetization_progress
from clipforge.publishing import meta as meta_adapter
from clipforge.publishing import tiktok as tiktok_adapter
from clipforge.publishing import youtube as youtube_adapter
from clipforge.scheduler import run_due_posts
from clipforge.seo import generate_all as generate_all_seo


# ------------------------------------------------------------------ schemas
class JobCreate(BaseModel):
    source_type: str = Field(default="local", pattern="^(local|youtube)$")
    source_url: str | None = None
    source_path: str | None = None
    title: str = ""
    own_content: bool | None = None
    own_content_declaration: bool | None = None

    def declared(self) -> bool:
        return bool(self.own_content or self.own_content_declaration)


class CandidateTrim(BaseModel):
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)


class PostCreate(BaseModel):
    clip_candidate_id: str
    platform: str = Field(pattern="^(youtube|instagram|facebook|tiktok)$")
    account_id: str | None = None
    scheduled_for: datetime | None = None
    video_path: str | None = None
    video_url: str | None = None
    seo: dict[str, Any] | None = None


class AccountCreate(BaseModel):
    provider: str = Field(pattern="^(youtube|instagram|facebook|tiktok)$")
    label: str = ""
    account_ref: str = ""
    scopes: list[str] = []
    token_env_var: str = ""  # NAME of the env var only — never a token value


# ------------------------------------------------------------------ app setup
_engine = init_db()
_Session = session_factory()


def get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


app = FastAPI(title="ClipForge API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _job_out(job: Job, db: Session) -> dict:
    return {
        "id": job.id,
        "title": job.title,
        "source": job.source_url or job.source_path or "",
        "source_type": job.source_type,
        "status": job.status,
        "own_content": job.own_content,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "candidates": db.query(ClipCandidate).filter_by(job_id=job.id).count(),
    }


def _candidate_out(c: ClipCandidate, keyword: str) -> dict:
    seo = generate_all_seo(
        keyword or c.hook_line or "clip", c.hook_line or "", c.hook_line or "", c.duration_s or 30.0
    )
    return {
        "id": c.id,
        "job_id": c.job_id,
        "n": c.n,
        "score": c.score,
        "hook_rating": c.hook_rating,
        "hook_line": c.hook_line,
        "reason": c.reason,
        "transcript_excerpt": c.transcript_excerpt,
        "start_s": c.trim_start_s if c.trim_start_s is not None else c.start_s,
        "end_s": c.trim_end_s if c.trim_end_s is not None else c.end_s,
        "status": c.status,
        "file_path": c.file_path,
        "seo": {
            p: {"title": v["title"], "description": v["description"], "hashtags": v["hashtags"]}
            for p, v in seo.items()
        },
    }


def _post_out(p: Post) -> dict:
    seo = p.seo_json or {}
    return {
        "id": p.id,
        "clip_id": p.clip_candidate_id,
        "clip_candidate_id": p.clip_candidate_id,
        "account_id": p.account_id,
        "platform": p.platform,
        "caption": seo.get("caption") or seo.get("description") or seo.get("title") or "",
        "scheduled_at": p.scheduled_for.isoformat() if p.scheduled_for else None,
        "scheduled_for": p.scheduled_for.isoformat() if p.scheduled_for else None,
        "status": p.status,
        "seo": seo,
        "idempotency_key": p.idempotency_key,
        "attempts": p.attempts,
        "error": p.error,
        "publish_result": p.publish_result_json,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _account_out(a: SocialAccount) -> dict:
    return {
        "id": a.id,
        "provider": a.provider,
        "label": a.label,
        "account_ref": a.account_ref,
        "scopes": a.scopes,
        "token_env_var": a.token_env_var,
        "status": a.status,
        "daily_cap_override": a.daily_cap_override,
        "token_configured": bool(a.token_env_var and os.environ.get(a.token_env_var)),
    }


def _get_or_404(db: Session, model, obj_id: str, name: str):
    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, f"{name} {obj_id} not found")
    return obj


# ------------------------------------------------------------------ health
@app.get("/health")
def health():
    return {
        "status": "ok",
        "dry_run": settings.dry_run,
        "version": "0.1.0",
        "database_url_scheme": settings.database_url.split("://")[0],
    }


# ------------------------------------------------------------------ jobs
@app.post("/jobs", status_code=201)
def create_job(payload: JobCreate, db: Session = Depends(get_db)):
    if payload.source_type == "youtube" and not payload.declared():
        raise HTTPException(
            422,
            "YouTube ingest requires own_content_declaration=true: "
            "ClipForge only processes content you own or hold the rights to.",
        )
    if (
        payload.source_type == "local"
        and payload.source_path is None
        and payload.source_url is None
    ):
        raise HTTPException(422, "local jobs need source_path (or source_url)")
    job = Job(
        source_type=payload.source_type,
        source_url=payload.source_url,
        source_path=payload.source_path,
        title=payload.title,
        own_content=payload.declared(),
        status=JobStatus.PENDING.value,
    )
    db.add(job)
    db.commit()
    return _job_out(job, db)


@app.get("/jobs")
def list_jobs(db: Session = Depends(get_db)):
    return [_job_out(j, db) for j in db.query(Job).order_by(Job.created_at.desc()).all()]


@app.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)):
    return _job_out(_get_or_404(db, Job, job_id, "job"), db)


@app.get("/jobs/{job_id}/candidates")
def list_candidates(job_id: str, db: Session = Depends(get_db)):
    job = _get_or_404(db, Job, job_id, "job")
    keyword = (job.title or "").split(":")[0].strip() or "clip"
    cands = db.query(ClipCandidate).filter_by(job_id=job_id).order_by(ClipCandidate.n.asc()).all()
    return [_candidate_out(c, keyword) for c in cands]


def _candidate_by_n(db: Session, job_id: str, n: int) -> ClipCandidate:
    _get_or_404(db, Job, job_id, "job")
    cand = db.query(ClipCandidate).filter_by(job_id=job_id, n=n).first()
    if cand is None:
        raise HTTPException(404, f"candidate {n} not found for job {job_id}")
    return cand


@app.post("/jobs/{job_id}/candidates/{n}/approve")
def approve_candidate(job_id: str, n: int, db: Session = Depends(get_db)):
    cand = _candidate_by_n(db, job_id, n)
    cand.status = "approved"
    db.commit()
    return {"id": cand.id, "n": cand.n, "status": cand.status}


@app.post("/jobs/{job_id}/candidates/{n}/reject")
def reject_candidate(job_id: str, n: int, db: Session = Depends(get_db)):
    cand = _candidate_by_n(db, job_id, n)
    cand.status = "rejected"
    db.commit()
    return {"id": cand.id, "n": cand.n, "status": cand.status}


@app.post("/jobs/{job_id}/candidates/{n}/trim")
def trim_candidate(job_id: str, n: int, payload: CandidateTrim, db: Session = Depends(get_db)):
    cand = _candidate_by_n(db, job_id, n)
    if payload.end_s <= payload.start_s:
        raise HTTPException(422, "trim end_s must be greater than start_s")
    if payload.end_s > cand.end_s or payload.start_s < cand.start_s:
        raise HTTPException(422, "trim range must lie inside the candidate's start/end")
    cand.trim_start_s, cand.trim_end_s = payload.start_s, payload.end_s
    db.commit()
    return {
        "id": cand.id,
        "n": cand.n,
        "start_s": cand.trim_start_s,
        "end_s": cand.trim_end_s,
        "duration_s": cand.duration_s,
    }


# ------------------------------------------------------------------ posts
@app.post("/posts", status_code=201)
def create_post(payload: PostCreate, db: Session = Depends(get_db)):
    cand = _get_or_404(db, ClipCandidate, payload.clip_candidate_id, "clip candidate")
    if cand.status != "approved":
        raise HTTPException(
            409,
            f"Approval-first: candidate {cand.id} is '{cand.status}', not "
            "'approved'. Approve it in review before creating a post.",
        )
    if payload.account_id:
        _get_or_404(db, SocialAccount, payload.account_id, "account")
    seo = payload.seo
    if seo is None:
        job = db.get(Job, cand.job_id)
        keyword = ((job.title if job else "") or "").split(":")[0].strip() or "clip"
        seo = generate_all_seo(
            keyword, cand.hook_line or "", cand.hook_line or "", cand.duration_s or 30.0
        ).get(payload.platform, {})
    post = Post(
        clip_candidate_id=cand.id,
        account_id=payload.account_id,
        platform=payload.platform,
        scheduled_for=payload.scheduled_for,
        status=PostStatus.PENDING_APPROVAL.value,
        seo_json=seo,
        video_path=payload.video_path or cand.file_path,
        video_url=payload.video_url,
    )
    db.add(post)
    db.commit()
    return _post_out(post)


@app.get("/posts")
def list_posts(
    status: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    q = db.query(Post).order_by(Post.created_at.desc())
    if status:
        q = q.filter_by(status=status)
    if platform:
        q = q.filter_by(platform=platform)
    return [_post_out(p) for p in q.all()]


@app.get("/posts/{post_id}")
def get_post(post_id: str, db: Session = Depends(get_db)):
    return _post_out(_get_or_404(db, Post, post_id, "post"))


@app.post("/posts/{post_id}/approve")
def approve_post(post_id: str, db: Session = Depends(get_db)):
    post = _get_or_404(db, Post, post_id, "post")
    if post.status not in {PostStatus.PENDING_APPROVAL.value, PostStatus.DRAFT.value}:
        raise HTTPException(409, f"post is '{post.status}', cannot approve from here")
    post.status = PostStatus.SCHEDULED.value if post.scheduled_for else PostStatus.APPROVED.value
    db.commit()
    return _post_out(post)


def _publish_post(post: Post, dry_run: bool, db: Session) -> dict:
    if post.status not in {
        PostStatus.APPROVED.value,
        PostStatus.SCHEDULED.value,
        PostStatus.DRAFT.value,
        PostStatus.PENDING_APPROVAL.value,
    }:
        raise HTTPException(409, f"post is '{post.status}' — approve it before publishing")
    if post.status in {PostStatus.DRAFT.value, PostStatus.PENDING_APPROVAL.value}:
        raise HTTPException(409, "Approval-first: approve this post before publishing it")
    if not dry_run and settings.dry_run:
        raise HTTPException(
            409,
            "Live publishing is disabled: CLIPFORGE_DRY_RUN=true. "
            "Set CLIPFORGE_DRY_RUN=false AND provide credentials to go live.",
        )
    seo = post.seo_json or {}
    title = seo.get("title") or seo.get("caption") or "ClipForge clip"
    description = seo.get("description") or seo.get("caption") or title
    try:
        if post.platform == "youtube":
            result = youtube_adapter.publish(
                title=title,
                description=description,
                video_path=post.video_path or "",
                publish_at=post.scheduled_for,
                dry_run=dry_run,
            )
        elif post.platform == "instagram":
            result = meta_adapter.publish_instagram(
                caption=description,
                video_url=post.video_url,
                video_path=post.video_path,
                dry_run=dry_run,
            )
        elif post.platform == "facebook":
            result = meta_adapter.publish_facebook(
                caption=description,
                video_url=post.video_url,
                video_path=post.video_path,
                dry_run=dry_run,
            )
        elif post.platform == "tiktok":
            result = tiktok_adapter.publish_tiktok(
                title=title,
                video_url=post.video_url,
                video_path=post.video_path,
                mode="upload",
                dry_run=dry_run,
            )
        else:
            raise HTTPException(400, f"unknown platform {post.platform!r}")
    except Exception as exc:
        post.status = PostStatus.FAILED.value
        post.error = f"{type(exc).__name__}: {exc}"
        db.commit()
        raise HTTPException(502, post.error)
    post.publish_result_json = result.to_dict()
    post.attempts += 1
    if result.status == "dry_run":
        post.status = PostStatus.DRY_RUN.value
        post.published_at = datetime.now()
    elif result.status in {"published", "draft_created"}:
        post.status = PostStatus.PUBLISHED.value
        post.published_at = datetime.now()
    else:
        post.status = PostStatus.FAILED.value
        post.error = result.error
    db.commit()
    return _post_out(post)


@app.post("/posts/{post_id}/publish")
def publish_post(post_id: str, dry_run: bool = Query(default=True), db: Session = Depends(get_db)):
    post = _get_or_404(db, Post, post_id, "post")
    return _publish_post(post, dry_run=dry_run, db=db)


# ------------------------------------------------------------------ accounts
@app.get("/accounts")
def list_accounts(db: Session = Depends(get_db)):
    return [_account_out(a) for a in db.query(SocialAccount).all()]


@app.post("/accounts", status_code=201)
def create_account(payload: AccountCreate, db: Session = Depends(get_db)):
    # Never accept a token value: token_env_var is a NAME only. Reject anything
    # that looks like a pasted secret (long opaque value, spaces, "sk-"/"EAAB" prefixes).
    value = payload.token_env_var.strip()
    if value and (len(value) > 64 or " " in value or value.startswith(("sk-", "EAAB", "ya29."))):
        raise HTTPException(
            422,
            "token_env_var must be the NAME of an env var "
            "(e.g. META_ACCESS_TOKEN), never a token value.",
        )
    account = SocialAccount(
        provider=payload.provider,
        label=payload.label,
        account_ref=payload.account_ref,
        scopes=payload.scopes,
        token_env_var=value,
    )
    db.add(account)
    db.commit()
    return _account_out(account)


# ------------------------------------------------- monetization / analytics / scheduler
@app.get("/monetization")
def get_monetization(
    platform: str | None = Query(default=None),
    subscribers: float = Query(default=0),
    followers: float = Query(default=0),
    watch_hours: float = Query(default=0),
    shorts_views: float = Query(default=0),
    views_30d: float = Query(default=0),
    account_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    metrics = {
        "subscribers": subscribers,
        "followers": followers,
        "watch_hours_12mo": watch_hours,
        "shorts_views_90d": shorts_views,
        "views_30d": views_30d,
    }
    rows = monetization_progress(metrics, platform)
    if account_id:  # cache the snapshot for the account
        for row in rows:
            db.add(
                MonetizationProgress(
                    account_id=account_id,
                    platform=row["platform"],
                    program=row["program"],
                    progress_pct=row["progress_pct"],
                    current_json=row["current"],
                    threshold_json=row["threshold"],
                    note=row["note"],
                )
            )
        db.commit()
    return rows


@app.get("/analytics")
def list_analytics(db: Session = Depends(get_db)):
    snaps = (
        db.query(AnalyticsSnapshot).order_by(AnalyticsSnapshot.created_at.desc()).limit(100).all()
    )
    return [
        {
            "account_id": s.account_id,
            "platform": s.platform,
            "date": s.date.isoformat() if s.date else None,
            "views": s.views,
            "followers": s.followers,
            "watch_hours": s.watch_hours,
            "shorts_views_90d": s.shorts_views_90d,
            "completion_pct": s.completion_pct,
            "viewed_pct": s.viewed_pct,
            "signals": retention_signals(s)["signals"],
        }
        for s in snaps
    ]


@app.get("/analytics/{account_id}")
def get_analytics(account_id: str, db: Session = Depends(get_db)):
    snaps = (
        db.query(AnalyticsSnapshot)
        .filter_by(account_id=account_id)
        .order_by(AnalyticsSnapshot.created_at.desc())
        .all()
    )
    if not snaps:
        # Credential-gated ingest stubs — dry-run safe, store nothing.
        return {"account_id": account_id, "snapshots": [], "ingest": ingest_all(db, account_id)}
    return {
        "account_id": account_id,
        "snapshots": [
            {
                "platform": s.platform,
                "views": s.views,
                "followers": s.followers,
                "watch_hours": s.watch_hours,
                "signals": retention_signals(s)["signals"],
            }
            for s in snaps
        ],
    }


@app.post("/scheduler/run")
def run_scheduler(dry_run: bool = Query(default=True), db: Session = Depends(get_db)):
    if not dry_run and settings.dry_run:
        raise HTTPException(409, "Live publishing disabled: CLIPFORGE_DRY_RUN=true")
    return {"dry_run": dry_run, "results": run_due_posts(db, dry_run=dry_run)}


__all__ = ["app"]
