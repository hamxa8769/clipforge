"""ClipForge API v0.1.1 — FastAPI app.

Endpoints (all dry-run by default; nothing publishes live without an
approved Post record, explicit credentials, and ``dry_run=false``):

  GET  /health
  POST /jobs                         (own_content declaration required)
  GET  /jobs | GET /jobs/{id} | GET /jobs/{id}/candidates
  POST /jobs/{id}/candidates/{n}/approve | reject | trim
  GET  /media/clips/{job_id}/{filename} | /media/uploads/{filename}
                                     (serve rendered clips / source uploads
                                      for the web player's <video> tag)
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

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from clipforge import __version__ as _VERSION  # noqa: F401  (package version)
from clipforge.abtest import (
    ABTestError,
    assign_variant_to_post,
)
from clipforge.abtest import (
    create_variant as ab_create_variant,
)
from clipforge.abtest import (
    pick_variant as ab_pick_variant,
)
from clipforge.abtest import (
    record_outcome as ab_record_outcome,
)
from clipforge.abtest import (
    variant_results as ab_variant_results,
)
from clipforge.analytics import ingest_all, retention_signals
from clipforge.brandkits import (
    BrandKitError,
    BrandKitSpec,
    apply_kit_to_seo,
    builtin_template,
    list_builtin_templates,
    validate_kit,
)
from clipforge.broll import BrollError, BrollOverlay, overlay_spec, validate_broll
from clipforge.config import settings
from clipforge.csv_import import import_csv as csv_import
from clipforge.db import (
    AnalyticsSnapshot,
    BrandKit,
    BrollAsset,
    ClipCandidate,
    Job,
    JobStatus,
    MonetizationProgress,
    Post,
    PostStatus,
    RetentionPoint,
    SocialAccount,
    TitleVariant,
    init_db,
    session_factory,
)
from clipforge.monetization import progress as monetization_progress
from clipforge.publishing import meta as meta_adapter
from clipforge.publishing import tiktok as tiktok_adapter
from clipforge.publishing import youtube as youtube_adapter
from clipforge.retention import (
    RetentionError,
    RetentionInput,
    adjust_candidate_scores,
    feedback_from_points,
    feedback_with_signals,
    point_to_dict,
    store_retention_point,
)
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
    brand_kit_id: str | None = None
    title_variant_id: str | None = None


class BrandKitCreate(BaseModel):
    name: str
    primary_colour: str | None = None
    secondary_colour: str | None = None
    accent_colour: str | None = None
    font_name: str | None = None
    caption_style: str | None = None
    logo_path: str | None = None
    intro_text: str | None = None
    outro_text: str | None = None
    template: str | None = None  # clone a built-in template, then override

    def to_spec(self) -> BrandKitSpec:
        base = builtin_template(self.template) if self.template else BrandKitSpec(name=self.name)
        data = base.to_dict()
        data["name"] = self.name
        for key in (
            "primary_colour",
            "secondary_colour",
            "accent_colour",
            "font_name",
            "caption_style",
            "logo_path",
            "intro_text",
            "outro_text",
        ):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return validate_kit(BrandKitSpec(**data))


class BrandKitUpdate(BaseModel):
    name: str | None = None
    primary_colour: str | None = None
    secondary_colour: str | None = None
    accent_colour: str | None = None
    font_name: str | None = None
    caption_style: str | None = None
    logo_path: str | None = None
    intro_text: str | None = None
    outro_text: str | None = None


class TitleVariantCreate(BaseModel):
    platform: str = Field(pattern="^(youtube|instagram|facebook|tiktok)$")
    title: str
    is_control: bool = False


class VariantOutcome(BaseModel):
    views: int | None = None
    avg_view_duration_s: float | None = None
    completion_pct: float | None = None
    viewed_pct: float | None = None


class BrollCreate(BaseModel):
    source_type: str
    source_path: str
    declaration: str
    source_start_s: float = 0.0
    source_end_s: float | None = None
    clip_start_s: float = 0.0
    clip_end_s: float | None = None
    position: str = "picture-in-picture"
    width_frac: float = 0.45


class RetentionCreate(BaseModel):
    platform: str
    clip_duration_s: float
    avg_view_duration_s: float | None = None
    completion_pct: float | None = None
    viewed_pct: float | None = None
    dropoff_second: float | None = None
    views: int = 0
    post_id: str | None = None
    clip_candidate_id: str | None = None
    source: str = "manual"


class FeedbackPreview(BaseModel):
    candidates: list[dict[str, Any]]
    points: list[dict[str, Any]] = []


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


app = FastAPI(title="ClipForge API", version="0.1.1")
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
        "title_variant_id": p.title_variant_id,
        "brand_kit_id": p.brand_kit_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _brand_kit_out(k: BrandKit) -> dict:
    return {
        "id": k.id,
        "name": k.name,
        "primary_colour": k.primary_colour,
        "secondary_colour": k.secondary_colour,
        "accent_colour": k.accent_colour,
        "font_name": k.font_name,
        "caption_style": k.caption_style,
        "logo_path": k.logo_path,
        "intro_text": k.intro_text,
        "outro_text": k.outro_text,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    }


def _kit_spec(db: Session, kit_id: str | None) -> BrandKitSpec | None:
    if not kit_id:
        return None
    kit = db.get(BrandKit, kit_id)
    if kit is None:
        raise HTTPException(404, f"brand kit {kit_id} not found")
    return BrandKitSpec(
        name=kit.name,
        primary_colour=kit.primary_colour,
        secondary_colour=kit.secondary_colour,
        accent_colour=kit.accent_colour,
        font_name=kit.font_name,
        caption_style=kit.caption_style,
        logo_path=kit.logo_path,
        intro_text=kit.intro_text,
        outro_text=kit.outro_text,
    )


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
        "version": "0.1.1",
        "database_url_scheme": settings.database_url.split("://")[0],
    }


# ------------------------------------------------------------------ media
# Read-only file serving for the web player's <video> tag. The frontend
# only ever receives server-side paths (candidate file_path, job source),
# which a browser cannot play — these endpoints turn them into URLs.
# Deliberately narrow: only .mp4/.webm/.mov under data/clips/<job_id>/ and
# data/uploads/, filenames only (no sub-paths, no traversal). Nothing
# outside the data dir is reachable; unknown files are 404, not 403,
# so existence is not leaked either way.
_MEDIA_EXTS = {".mp4", ".webm", ".mov"}
_MEDIA_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}


def _data_dir() -> Path:
    # Read per request (not from the frozen settings snapshot) so tests can
    # point the media root at a temp dir via CLIPFORGE_DATA_DIR.
    return Path(os.environ.get("CLIPFORGE_DATA_DIR") or settings.data_dir)


def _media_file(subdir: str, *parts: str) -> Path:
    root = (_data_dir() / subdir).resolve()
    target = (root.joinpath(*parts)).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(404, "file not found")
    if target.suffix.lower() not in _MEDIA_EXTS or not target.is_file():
        raise HTTPException(404, "file not found")
    return target


@app.get("/media/clips/{job_id}/{filename}")
def media_clip(job_id: str, filename: str):
    path = _media_file("clips", job_id, filename)
    return FileResponse(path, media_type=_MEDIA_TYPES[path.suffix.lower()])


@app.get("/media/uploads/{filename}")
def media_upload(filename: str):
    path = _media_file("uploads", filename)
    return FileResponse(path, media_type=_MEDIA_TYPES[path.suffix.lower()])


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
    else:
        seo = dict(seo)
    # Brand kit: styles the SEO copy (intro/outro + kit metadata) and
    # records the caption style the renderer should use.
    kit = _kit_spec(db, payload.brand_kit_id)
    if kit is not None:
        seo = apply_kit_to_seo(seo, kit)
    # A/B title: an explicit variant wins; otherwise the least-used
    # variant for this clip/platform is auto-assigned (balanced).
    variant_id = payload.title_variant_id
    if variant_id:
        variant = _get_or_404(db, TitleVariant, variant_id, "title variant")
        if variant.clip_candidate_id != cand.id or variant.platform != payload.platform:
            raise HTTPException(
                422,
                f"title variant {variant_id} does not belong to "
                f"clip {cand.id} on {payload.platform}",
            )
        seo["title"] = variant.title
    else:
        picked = ab_pick_variant(db, cand.id, payload.platform)
        if picked is not None:
            variant_id = picked.id
            seo["title"] = picked.title
    post = Post(
        clip_candidate_id=cand.id,
        account_id=payload.account_id,
        platform=payload.platform,
        scheduled_for=payload.scheduled_for,
        status=PostStatus.PENDING_APPROVAL.value,
        seo_json=seo,
        video_path=payload.video_path or cand.file_path,
        video_url=payload.video_url,
        brand_kit_id=payload.brand_kit_id,
        title_variant_id=variant_id,
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


@app.post("/scheduler/run")
def run_scheduler(dry_run: bool = Query(default=True), db: Session = Depends(get_db)):
    if not dry_run and settings.dry_run:
        raise HTTPException(409, "Live publishing disabled: CLIPFORGE_DRY_RUN=true")
    return {"dry_run": dry_run, "results": run_due_posts(db, dry_run=dry_run)}


# ------------------------------------------------------------------ brand kits
@app.get("/brand-kits/templates")
def list_brand_kit_templates():
    """The built-in templates (no DB row needed to use one as a base)."""
    return list_builtin_templates()


@app.post("/brand-kits", status_code=201)
def create_brand_kit(payload: BrandKitCreate, db: Session = Depends(get_db)):
    try:
        spec = payload.to_spec()
    except BrandKitError as exc:
        raise HTTPException(422, str(exc))
    kit = BrandKit(**spec.to_dict())
    db.add(kit)
    db.commit()
    return _brand_kit_out(kit)


@app.get("/brand-kits")
def list_brand_kits(db: Session = Depends(get_db)):
    return [_brand_kit_out(k) for k in db.query(BrandKit).order_by(BrandKit.created_at.asc()).all()]


@app.get("/brand-kits/{kit_id}")
def get_brand_kit(kit_id: str, db: Session = Depends(get_db)):
    return _brand_kit_out(_get_or_404(db, BrandKit, kit_id, "brand kit"))


@app.put("/brand-kits/{kit_id}")
def update_brand_kit(kit_id: str, payload: BrandKitUpdate, db: Session = Depends(get_db)):
    kit = _get_or_404(db, BrandKit, kit_id, "brand kit")
    spec = BrandKitSpec(
        name=kit.name,
        primary_colour=kit.primary_colour,
        secondary_colour=kit.secondary_colour,
        accent_colour=kit.accent_colour,
        font_name=kit.font_name,
        caption_style=kit.caption_style,
        logo_path=kit.logo_path,
        intro_text=kit.intro_text,
        outro_text=kit.outro_text,
    )
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(spec, key, value)
    try:
        validate_kit(spec)
    except BrandKitError as exc:
        raise HTTPException(422, str(exc))
    for key, value in spec.to_dict().items():
        setattr(kit, key, value)
    db.commit()
    return _brand_kit_out(kit)


@app.delete("/brand-kits/{kit_id}")
def delete_brand_kit(kit_id: str, db: Session = Depends(get_db)):
    kit = _get_or_404(db, BrandKit, kit_id, "brand kit")
    if db.query(Post).filter_by(brand_kit_id=kit_id).first() is not None:
        raise HTTPException(409, "brand kit is in use by scheduled posts")
    db.delete(kit)
    db.commit()
    return {"id": kit_id, "deleted": True}


# ------------------------------------------------------------------ A/B title variants
@app.post("/clips/{candidate_id}/title-variants", status_code=201)
def create_title_variant(
    candidate_id: str, payload: TitleVariantCreate, db: Session = Depends(get_db)
):
    _get_or_404(db, ClipCandidate, candidate_id, "clip candidate")
    try:
        variant = ab_create_variant(
            db,
            clip_candidate_id=candidate_id,
            platform=payload.platform,
            title=payload.title,
            is_control=payload.is_control,
        )
    except ABTestError as exc:
        raise HTTPException(422, str(exc))
    return {
        "id": variant.id,
        "clip_candidate_id": variant.clip_candidate_id,
        "platform": variant.platform,
        "title": variant.title,
        "is_control": variant.is_control,
    }


@app.get("/clips/{candidate_id}/title-variants")
def list_title_variants(
    candidate_id: str,
    platform: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _get_or_404(db, ClipCandidate, candidate_id, "clip candidate")
    if platform and platform not in {"youtube", "instagram", "facebook", "tiktok"}:
        raise HTTPException(422, f"unknown platform {platform!r}")
    if platform:
        return ab_variant_results(db, candidate_id, platform)
    return [
        row
        for p in ("youtube", "instagram", "facebook", "tiktok")
        for row in ab_variant_results(db, candidate_id, p)
    ]


@app.post("/posts/{post_id}/title-variant/{variant_id}")
def assign_post_variant(post_id: str, variant_id: str, db: Session = Depends(get_db)):
    try:
        post = assign_variant_to_post(db, post_id, variant_id)
    except ABTestError as exc:
        raise HTTPException(422, str(exc))
    return _post_out(post)


@app.post("/title-variants/{variant_id}/outcome")
def record_variant_outcome(variant_id: str, payload: VariantOutcome, db: Session = Depends(get_db)):
    try:
        variant = ab_record_outcome(
            db,
            variant_id,
            views=payload.views,
            avg_view_duration_s=payload.avg_view_duration_s,
            completion_pct=payload.completion_pct,
            viewed_pct=payload.viewed_pct,
        )
    except ABTestError as exc:
        raise HTTPException(422, str(exc))
    return {
        "id": variant.id,
        "title": variant.title,
        "views": variant.views,
        "avg_view_duration_s": variant.avg_view_duration_s,
        "completion_pct": variant.completion_pct,
        "viewed_pct": variant.viewed_pct,
    }


# ------------------------------------------------------------------ B-roll
@app.post("/clips/{candidate_id}/broll", status_code=201)
def add_broll(candidate_id: str, payload: BrollCreate, db: Session = Depends(get_db)):
    cand = _get_or_404(db, ClipCandidate, candidate_id, "clip candidate")
    try:
        overlay = validate_broll(
            source_type=payload.source_type,
            source_path=payload.source_path,
            declaration=payload.declaration,
            source_start_s=payload.source_start_s,
            source_end_s=payload.source_end_s,
            clip_start_s=payload.clip_start_s,
            clip_end_s=payload.clip_end_s,
            position=payload.position,
            width_frac=payload.width_frac,
            clip_duration_s=cand.duration_s,
        )
    except BrollError as exc:
        raise HTTPException(422, str(exc))
    asset = BrollAsset(clip_candidate_id=cand.id, **overlay.to_dict())
    db.add(asset)
    db.commit()
    return {
        "id": asset.id,
        "clip_candidate_id": asset.clip_candidate_id,
        **overlay_spec(overlay, cand.duration_s),
    }


@app.get("/clips/{candidate_id}/broll")
def list_broll(candidate_id: str, db: Session = Depends(get_db)):
    cand = _get_or_404(db, ClipCandidate, candidate_id, "clip candidate")
    out = []
    for asset in db.query(BrollAsset).filter_by(clip_candidate_id=cand.id).all():
        overlay = BrollOverlay(
            source_type=asset.source_type,
            source_path=asset.source_path,
            declaration=asset.declaration,
            source_start_s=asset.source_start_s,
            source_end_s=asset.source_end_s,
            clip_start_s=asset.clip_start_s,
            clip_end_s=asset.clip_end_s,
            position=asset.position,
            width_frac=asset.width_frac,
        )
        out.append({"id": asset.id, **overlay_spec(overlay, cand.duration_s)})
    return out


@app.delete("/clips/{candidate_id}/broll/{asset_id}")
def delete_broll(candidate_id: str, asset_id: str, db: Session = Depends(get_db)):
    asset = db.get(BrollAsset, asset_id)
    if asset is None or asset.clip_candidate_id != candidate_id:
        raise HTTPException(404, f"b-roll asset {asset_id} not found for clip {candidate_id}")
    db.delete(asset)
    db.commit()
    return {"id": asset_id, "deleted": True}


# ------------------------------------------------------------------ bulk / CSV scheduling
@app.post("/posts/bulk/csv")
async def bulk_import_csv(
    file: UploadFile = File(...),
    dry_run: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    """Import scheduled posts from CSV. ``dry_run=true`` (default) only
    previews: it validates every row and reports per-row errors without
    storing anything. ``dry_run=false`` stores the valid rows as
    ``pending_approval`` posts (approval-first still applies) and
    skips rows already imported (idempotency key)."""
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "CSV must be UTF-8 encoded")
    return csv_import(db, text, dry_run=dry_run).to_dict()


# ------------------------------------------------------------------ retention analytics
@app.post("/analytics/retention", status_code=201)
def ingest_retention(payload: RetentionCreate, db: Session = Depends(get_db)):
    """Store one retention observation (manual / platform-export data).

    There is NO live platform feed behind this endpoint in this
    release — values are supplied by the caller (manual entry or a
    platform export file). Live YouTube/Meta/TikTok analytics ingest
    still needs credentials; see docs/phase2.md.
    """
    try:
        point = store_retention_point(
            db,
            RetentionInput(
                platform=payload.platform,
                clip_duration_s=payload.clip_duration_s,
                avg_view_duration_s=payload.avg_view_duration_s,
                completion_pct=payload.completion_pct,
                viewed_pct=payload.viewed_pct,
                dropoff_second=payload.dropoff_second,
                views=payload.views,
                post_id=payload.post_id,
                clip_candidate_id=payload.clip_candidate_id,
                source=payload.source,
            ),
        )
    except RetentionError as exc:
        raise HTTPException(422, str(exc))
    return point_to_dict(point)


@app.get("/analytics/retention")
def list_retention(
    platform: str | None = Query(default=None),
    clip_candidate_id: str | None = Query(default=None),
    post_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    query = db.query(RetentionPoint).order_by(RetentionPoint.created_at.desc())
    if platform:
        query = query.filter_by(platform=platform)
    if clip_candidate_id:
        query = query.filter_by(clip_candidate_id=clip_candidate_id)
    if post_id:
        query = query.filter_by(post_id=post_id)
    return [point_to_dict(p) for p in query.limit(200).all()]


@app.get("/analytics/feedback")
def get_retention_feedback(
    platform: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """Aggregate feedback from stored retention points (all platforms
    unless ``platform`` is given). Empty when no points exist yet."""
    query = db.query(RetentionPoint)
    if platform:
        query = query.filter_by(platform=platform)
    return feedback_from_points(query.all()).to_dict()


@app.post("/analytics/feedback/preview")
def preview_feedback(payload: FeedbackPreview):
    """Apply retention feedback to a candidate batch (pure preview).

    ``candidates`` are ``{score, hook, signals, duration_s, ...}``;
    ``points`` are retention dicts (with optional ``signals``/``hook``
    per point for signal boosts). Nothing is stored.
    """
    try:
        feedback = feedback_with_signals(payload.points)
    except (RetentionError, TypeError, ValueError) as exc:
        raise HTTPException(422, f"invalid retention point: {exc}")
    return {
        "feedback": feedback.to_dict(),
        "candidates": adjust_candidate_scores(payload.candidates, feedback),
    }


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


__all__ = ["app"]
