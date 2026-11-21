"""ClipForge API v0.6.0 — FastAPI app.

Endpoints (all dry-run by default; nothing publishes live without an
approved Post record, explicit credentials, and ``dry_run=false``):

  GET  /health
  GET  /dependencies                 (doctor: tools / keys / flags present?)
  POST /jobs                         (own_content declaration required)
  POST /jobs/upload                  (multipart file upload → job)
  GET  /jobs | GET /jobs/{id} | GET /jobs/{id}/candidates
  GET  /jobs/{id}/logs
  POST /jobs/{id}/run | retry | cancel
                                     (live workflow: stage history, progress,
                                      ETA, retry/cancel)
  POST /jobs/{id}/candidates/{n}/approve | reject | trim
  GET  /clips/{candidate_id}/seo     (stored per-platform SEO variants)
  GET  /media/clips/{job_id}/{filename} | /media/uploads/{filename}
                                     (serve rendered clips / source uploads
                                      for the web player's <video> tag)
  POST /posts                        (from an APPROVED clip only)
  GET  /posts?status=                (approval queue)
  POST /posts/{id}/approve | POST /posts/{id}/publish (dry-run default)
  GET  /posts/{id}
  GET  /accounts | POST /accounts    (token stored as env-var NAME only)
  GET  /channels                     (per-platform connection status)
  GET  /connect/guide/{platform}     (guided-connect steps per platform)
  GET  /onboarding | PUT /onboarding (first-run progress, resumable)
  GET  /auth/{platform}/start        (OAuth authorize URL)
  GET  /auth/callback/{platform}     (OAuth callback — exchanges the code)
  POST /channels/{platform}/refresh  (refresh / extend the stored token)
  POST /channels/{platform}/disconnect
  GET  /settings | PUT /settings     (masked credentials + preferences)
  POST /settings/test-connection/{service}  (live auth check per service)
  GET  /ffmpeg/status | POST /ffmpeg/install
                                     (one-click FFmpeg install: official
                                      build into <data_dir>/ffmpeg, progress
                                      pollable, PATH updated on Windows)
  GET  /monetization                 (tracker; ?platform=&subs=&...)
  GET  /analytics | GET /analytics/{account_id}
  POST /scheduler/run                (run due posts; dry-run default)
  GET  /worker/queue | POST /worker/run-next
                                     (background worker: running / queued /
                                      next / recent runs)
  GET  /automation/feed | GET /automation/rules | PUT /automation/rules
  POST /automation/rules | PATCH /automation/rules/{id}
  POST /automation/run
  POST /automation/pick-next/{job_id}
                                     (automation rules + history)
  GET  /scheduler/status             (runner state: running / last run /
                                      due now / last error)

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

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from clipforge import __version__ as _VERSION  # noqa: F401  (package version)
from clipforge import agent as agent_mod
from clipforge import automation
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
from clipforge.auth import (
    OAuthError,
    SettingsStore,
    StateStore,
    TokenRecord,
    TokenStore,
)
from clipforge.auth.channels import all_channels, channel_status
from clipforge.auth.connect_guide import connect_guide
from clipforge.auth.oauth import (
    build_authorize_url,
    exchange_code,
    fetch_profile,
    refresh_access_token,
)
from clipforge.auth.providers import PLATFORMS, get_provider, redirect_uri
from clipforge.auth.secrets import SecretStore
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
    AutomationEvent,
    AutomationRule,
    BrandKit,
    BrollAsset,
    CandidateSeo,
    ClipCandidate,
    Job,
    JobStageEvent,
    JobStatus,
    MonetizationProgress,
    Post,
    PostStatus,
    RetentionPoint,
    SchedulerRun,
    SocialAccount,
    TitleVariant,
    WorkerRun,
    init_db,
    session_factory,
)
from clipforge.ffmpeg_install import FfmpegInstaller, ensure_bundled_on_path
from clipforge.llm import (
    GEMINI_DEFAULT_MODEL,
    GEMINI_MODELS,
    resolve_engine,
    test_gemini,
)
from clipforge.monetization import progress as monetization_progress
from clipforge.niches import PRESET_NAMES
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
from clipforge.scheduler import due_posts, run_due_posts
from clipforge.seo import generate_all as generate_all_seo
from clipforge.worker import RUNNABLE_STATUSES, queue_summary, run_job, run_next_queued
from clipforge.workflow import (
    check_dependencies,
    estimate_eta_s,
    score_band,
    stage_event_out,
    stage_events_for,
)


# ------------------------------------------------------------------ schemas
class JobCreate(BaseModel):
    source_type: str = Field(default="local", pattern="^(local|youtube)$")
    source_url: str | None = None
    source_path: str | None = None
    title: str = ""
    own_content: bool | None = None
    own_content_declaration: bool | None = None
    # Worker options (stored on job.meta_json; see clipforge.worker).
    transcript_text: str | None = None  # deterministic fallback transcript
    top_n: int = Field(default=3, ge=1, le=10)
    caption_style: str = "bold-pop"

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


class SettingsUpdate(BaseModel):
    """Partial settings update. Omitted = keep; "" on a secret = clear it."""

    dry_run: bool | None = None
    default_privacy: dict[str, str] | None = None
    default_platforms: list[str] | None = None
    llm_provider: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None  # None = keep, "" = clear, value = set
    gemini_api_key: str | None = None  # v0.5.0: same semantics, Gemini key
    # {platform: {"client_id": ..., "client_secret": ...}} — same semantics.
    credentials: dict[str, dict[str, str | None]] | None = None


class OnboardingUpdate(BaseModel):
    """Partial onboarding update. Omitted = keep current value.

    ``llm`` is "" (undecided), "skipped" or "set"; the other fields are
    booleans. Unknown fields are rejected with a 422.
    """

    model_config = ConfigDict(extra="forbid")

    ffmpeg_acknowledged: bool | None = None
    llm: str | None = None
    first_channel_connected: bool | None = None
    sample_workflow_run: bool | None = None
    completed: bool | None = None


# ------------------------------------------------------------------ app setup
_engine = init_db()
_Session = session_factory()


def get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


app = FastAPI(title="ClipForge API", version=_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- v0.5.0 worker 3: setup-wizard state router (NEW FILE
# apps/api/clipforge_api/setup.py — GET/POST /api/setup/state only).
# It owns its own setup.json; it does NOT touch the agent engine or the
# LLM key store (worker 2 owns those). ---------------------------------
from clipforge_api.setup import router as setup_router  # noqa: E402

app.include_router(setup_router)

# --- v0.6.0: trending discovery + growth routers (Worker B/C).
# Registered here, before __main__ adds the "/" StaticFiles mount,
# so /api/trending/* and /growth/* are never swallowed by static serving.
from clipforge_api.growth import router as growth_router  # noqa: E402
from clipforge_api.trending import router as trending_router  # noqa: E402

app.include_router(growth_router)
app.include_router(trending_router)


def _job_out(job: Job, db: Session, detail: bool = False) -> dict:
    """Job summary. ``detail=True`` adds the live-workflow view: stage
    history, worker runs, alternates below the score threshold."""
    meta = dict(job.meta_json or {})
    out: dict = {
        "id": job.id,
        "title": job.title,
        "source": job.source_url or job.source_path or "",
        "source_type": job.source_type,
        "status": job.status,
        "own_content": job.own_content,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "candidates": db.query(ClipCandidate).filter_by(job_id=job.id).count(),
        # Live workflow fields (v0.3.0) — always present, null/0 when idle.
        "stage": meta.get("stage"),
        "progress_pct": float(meta.get("progress_pct") or 0.0),
        "eta_s": estimate_eta_s(db, job),
        "demo": bool(meta.get("demo", False)),
        "blocked_by": meta.get("blocked_by"),
    }
    if detail:
        events = stage_events_for(db, job.id)
        out["stages"] = [stage_event_out(e) for e in events]
        runs = (
            db.query(WorkerRun)
            .filter_by(job_id=job.id)
            .order_by(WorkerRun.started_at.desc())
            .limit(5)
            .all()
        )
        out["worker_runs"] = [
            {
                "id": r.id,
                "status": r.status,
                "outcome": r.outcome,
                "error": r.error,
                "duration_s": r.duration_s,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            }
            for r in runs
        ]
        # Frontend contract (docs/API-CONTRACT.md §2): which stage failed
        # (if any), and the run's real start / finish / duration. All
        # null when the job never ran.
        failed_stage = None
        for event in reversed(events):
            if event.status in {"failed", "blocked"}:
                failed_stage = event.stage
                break
        latest_run = runs[0] if runs else None
        out["failed_stage"] = failed_stage
        out["started_at"] = (
            latest_run.started_at.isoformat() if latest_run and latest_run.started_at else None
        )
        out["finished_at"] = (
            latest_run.finished_at.isoformat() if latest_run and latest_run.finished_at else None
        )
        out["duration_s"] = latest_run.duration_s if latest_run else None
        alternates: list[dict] = []
        for event in reversed(events):
            if event.stage == "score" and (event.detail_json or {}).get("alternates"):
                alternates = (event.detail_json or {})["alternates"]
                break
        out["alternates"] = alternates
    return out


def _candidate_out(c: ClipCandidate, keyword: str) -> dict:
    seo = generate_all_seo(
        keyword or c.hook_line or "clip", c.hook_line or "", c.hook_line or "", c.duration_s or 30.0
    )
    return {
        "id": c.id,
        "job_id": c.job_id,
        "n": c.n,
        "score": c.score,
        "score_band": score_band(c.score),
        "hook_rating": c.hook_rating,
        "hook_line": c.hook_line,
        "reason": c.reason,
        "transcript_excerpt": c.transcript_excerpt,
        "start_s": c.trim_start_s if c.trim_start_s is not None else c.start_s,
        "end_s": c.trim_end_s if c.trim_end_s is not None else c.end_s,
        # Original span (the trim endpoint validates against it, so the
        # UI can widen a previous trim back out — contract gap, docs/api.md).
        "orig_start_s": c.start_s,
        "orig_end_s": c.end_s,
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
def _secrets() -> SecretStore:
    return SecretStore(_data_dir())


def _settings_store() -> SettingsStore:
    return SettingsStore(_data_dir(), _secrets())


def _effective_dry_run() -> bool:
    """Settings-page override wins; otherwise CLIPFORGE_DRY_RUN decides."""
    return _settings_store().effective_dry_run(settings.dry_run)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "dry_run": _effective_dry_run(),
        "version": _VERSION,
        "database_url_scheme": settings.database_url.split("://")[0],
    }


@app.get("/dependencies")
def get_dependencies():
    """Doctor check: every tool / package / key the pipeline may need.

    Presence only — no secret *value* is ever returned. A job blocked
    on a missing dependency names the same ``code`` you see here
    (e.g. ``ffmpeg_missing``, ``transcription_backend_missing``), so
    the UI can point at exactly what to install or set. The
    ``doctor``-command equivalent from artbyjazi/autoclip and
    ghost1412/shortsflow (see docs/REUSE.md).
    """
    deps = [d.to_dict() for d in check_dependencies()]
    return {
        "dependencies": deps,
        "missing": [d for d in deps if not d["available"]],
        "all_available": all(d["available"] for d in deps),
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
    meta: dict = {"top_n": payload.top_n, "caption_style": payload.caption_style}
    if payload.transcript_text:
        meta["transcript_text"] = payload.transcript_text
    job = Job(
        source_type=payload.source_type,
        source_url=payload.source_url,
        source_path=payload.source_path,
        title=payload.title,
        own_content=payload.declared(),
        status=JobStatus.PENDING.value,
        meta_json=meta,
    )
    db.add(job)
    db.commit()
    return _job_out(job, db)


@app.post("/jobs/upload", status_code=201)
async def upload_job(
    file: UploadFile = File(...),
    title: str = Form(default=""),
    own_content_declaration: bool = Form(default=False),
    transcript_text: str | None = Form(default=None),
    top_n: int = Form(default=3),
    db: Session = Depends(get_db),
):
    """Upload a source video file and create a job for it (ingest stage).

    Only .mp4/.mov, max 10 GiB (same caps as local ingest). The file is
    stored under ``data/uploads/`` with a random prefix; the job's
    ``source_path`` points at it. YouTube uploads are not accepted
    here — use POST /jobs with source_type=youtube + the declaration.
    """
    from clipforge.ingest import MAX_SOURCE_BYTES

    name = Path(file.filename or "upload").name
    if Path(name).suffix.lower() not in {".mp4", ".mov"}:
        raise HTTPException(422, "only .mp4 / .mov uploads are accepted")
    raw = await file.read()
    if len(raw) > MAX_SOURCE_BYTES:
        raise HTTPException(422, "file exceeds the 10 GiB source cap")
    import uuid as _uuid

    uploads = _data_dir() / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    stored = uploads / f"{_uuid.uuid4().hex[:12]}_{name}"
    stored.write_bytes(raw)
    meta: dict = {"top_n": max(1, min(10, top_n)), "caption_style": "bold-pop"}
    if transcript_text:
        meta["transcript_text"] = transcript_text
    job = Job(
        source_type="local",
        source_path=str(stored),
        title=title,
        own_content=bool(own_content_declaration),
        status=JobStatus.PENDING.value,
        meta_json=meta,
    )
    db.add(job)
    db.commit()
    return _job_out(job, db)


@app.get("/jobs")
def list_jobs(db: Session = Depends(get_db)):
    return [_job_out(j, db) for j in db.query(Job).order_by(Job.created_at.desc()).all()]


@app.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)):
    return _job_out(_get_or_404(db, Job, job_id, "job"), db, detail=True)


@app.get("/jobs/{job_id}/logs")
def get_job_logs(job_id: str, db: Session = Depends(get_db)):
    """Per-stage run log for a job (docs/API-CONTRACT.md §2).

    One entry per stage event, oldest first: ``{ts, stage, level,
    message}``. ``level`` is ``error`` for failed / blocked stages,
    ``info`` otherwise. Worker-run start/finish lines are included so
    the log also shows the run outcome and duration. Empty ``logs``
    when the job never ran — the job page then shows the single
    ``error`` string instead.
    """
    job = _get_or_404(db, Job, job_id, "job")
    logs: list[dict] = []
    for event in stage_events_for(db, job.id):
        logs.append(
            {
                "ts": event.created_at.isoformat() if event.created_at else None,
                "stage": event.stage,
                "level": "error" if event.status in {"failed", "blocked"} else "info",
                "message": event.message + (f" [{event.error_code}]" if event.error_code else ""),
            }
        )
    for run in (
        db.query(WorkerRun).filter_by(job_id=job.id).order_by(WorkerRun.started_at.asc()).all()
    ):
        logs.append(
            {
                "ts": run.started_at.isoformat() if run.started_at else None,
                "stage": "worker",
                "level": "info",
                "message": f"worker run started ({run.id})",
            }
        )
        if run.finished_at:
            logs.append(
                {
                    "ts": run.finished_at.isoformat(),
                    "stage": "worker",
                    "level": "error" if run.status in {"failed", "blocked"} else "info",
                    "message": f"worker run {run.status}: {run.outcome}",
                }
            )
    return {"job_id": job.id, "logs": logs}


@app.post("/jobs/{job_id}/run")
def run_job_endpoint(job_id: str, db: Session = Depends(get_db)):
    """Run a job through the worker now (synchronous) and return its detail.

    Only pending / queued / failed / blocked jobs can run; anything
    else is 409. A missing dependency leaves the job ``blocked`` with
    ``blocked_by`` naming it — the response is still 200 with the job
    detail, because the block is a state, not a request error.
    """
    job = _get_or_404(db, Job, job_id, "job")
    if job.status not in RUNNABLE_STATUSES:
        raise HTTPException(
            409,
            f"job is '{job.status}' — only {sorted(RUNNABLE_STATUSES)} jobs can run",
        )
    run_job(db, job_id)
    db.refresh(job)
    return _job_out(job, db, detail=True)


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, db: Session = Depends(get_db)):
    """Reset a failed / blocked job to pending and run it again.

    Old candidates + stored SEO are dropped (they are re-derived);
    stage history and worker runs are kept as the audit trail. A retry
    resumes scoring from the stage artifact when one exists
    (``data/jobs/<id>/candidates.json``) and transcription from the
    sha256 transcript cache — it never re-does completed work blindly.
    """
    job = _get_or_404(db, Job, job_id, "job")
    if job.status not in {JobStatus.FAILED.value, JobStatus.BLOCKED.value}:
        raise HTTPException(
            409,
            f"job is '{job.status}' — only failed / blocked jobs can be retried",
        )
    cand_ids = [c.id for c in db.query(ClipCandidate).filter_by(job_id=job.id).all()]
    if cand_ids:
        db.query(CandidateSeo).filter(CandidateSeo.clip_candidate_id.in_(cand_ids)).delete(
            synchronize_session=False
        )
    db.query(ClipCandidate).filter_by(job_id=job.id).delete()
    job.status = JobStatus.PENDING.value
    job.error = None
    job.meta_json = {
        k: v
        for k, v in (dict(job.meta_json or {}).items())
        if k in {"top_n", "caption_style", "transcript_text"}
    }
    db.commit()
    run_job(db, job_id)
    db.refresh(job)
    return _job_out(job, db, detail=True)


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, db: Session = Depends(get_db)):
    """Cancel a job that has not started (or that failed / blocked).

    A running in-process job cannot be interrupted mid-request; cancel
    applies to pending / queued / failed / blocked jobs. Any orphan
    ``running`` worker run for the job is closed as cancelled.
    """
    job = _get_or_404(db, Job, job_id, "job")
    if job.status not in {
        JobStatus.PENDING.value,
        JobStatus.QUEUED.value,
        JobStatus.FAILED.value,
        JobStatus.BLOCKED.value,
    }:
        raise HTTPException(409, f"job is '{job.status}' — cannot cancel from here")
    job.status = JobStatus.CANCELLED.value
    db.query(WorkerRun).filter_by(job_id=job.id, status="running").update(
        {"status": "cancelled", "outcome": "cancelled by user"}
    )
    db.commit()
    return _job_out(job, db, detail=True)


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
    job = db.get(Job, job_id)
    # The review stage completes on first approval: the job is ready
    # to schedule. Record it in the stage history (progress → 100).
    if job is not None and job.status == JobStatus.REVIEW.value:
        job.status = JobStatus.READY.value
        db.add(
            JobStageEvent(
                job_id=job.id,
                stage="review",
                status="done",
                progress_pct=100.0,
                message=f"candidate {n} approved — job ready to schedule",
            )
        )
        meta = dict(job.meta_json or {})
        meta.update({"stage": "review", "progress_pct": 100.0})
        job.meta_json = meta
        db.commit()
    # Automation rules fire here (SEO gen + auto-queue). They never
    # raise — a rule failure is logged, approval still stands.
    auto = automation.on_candidate_approved(db, cand)
    return {"id": cand.id, "n": cand.n, "status": cand.status, "automation": auto}


@app.get("/clips/{candidate_id}/seo")
def get_candidate_seo(candidate_id: str, db: Session = Depends(get_db)):
    """Stored per-platform SEO variants for a clip.

    Written by the worker (``seo`` stage) and by the
    ``auto_seo_on_approve`` rule. Empty ``{}`` when SEO was never
    generated for this clip — the review UI then falls back to the
    on-the-fly variants in ``GET /jobs/{id}/candidates``.
    """
    cand = _get_or_404(db, ClipCandidate, candidate_id, "clip candidate")
    return {
        "clip_candidate_id": cand.id,
        "seo": automation.seo_for_candidate(db, cand),
    }


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
    if not dry_run and _effective_dry_run():
        raise HTTPException(
            409,
            "Live publishing is disabled: dry-run is on. "
            "Turn dry-run off in Settings AND provide credentials to go live.",
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


# ------------------------------------------------- channels / OAuth / settings
# OAuth state tokens are single-use and in-memory (process-local sidecar).
_OAUTH_STATES = StateStore()


def _require_platform(platform: str):
    try:
        return get_provider(platform)
    except ValueError:
        raise HTTPException(404, f"unknown platform: {platform}") from None


def _callback_page(*, title: str, message: str, ok: bool) -> HTMLResponse:
    """Plain result page the platform redirects the browser to.

    The browser lands here after granting (or refusing) access; the page
    just says what happened — the user closes it and returns to ClipForge.
    """
    colour = "#22c55e" if ok else "#ef4444"
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8">
<title>ClipForge — {title}</title></head>
<body style="font-family:system-ui,sans-serif;background:#111;color:#eee;
padding:3rem 1.5rem;text-align:center">
<h1 style="color:{colour}">{title}</h1><p>{message}</p>
<p>You can close this window and return to ClipForge.</p>
</body></html>""",
        status_code=200 if ok else 400,
    )


@app.get("/channels")
def list_channels(db: Session = Depends(get_db)):
    """One status card per platform, from the real backend — never hardcoded.

    CANONICAL channel-status endpoint (v0.3.0). GET /accounts is the
    legacy account-record endpoint (list/create SocialAccount rows) and
    stays working for compatibility, but the Channels page and any new
    client must use /channels: only it knows OAuth token state, expiry,
    scopes granted, daily caps, and the review/audit notice.
    """
    return all_channels(secrets=_secrets(), db=db)


@app.get("/channels/{platform}")
def get_channel(platform: str, db: Session = Depends(get_db)):
    _require_platform(platform)
    return channel_status(platform, secrets=_secrets(), db=db)


@app.get("/connect/guide/{platform}")
def get_connect_guide(platform: str):
    """Plain-language setup guide for the Connect wizard.

    The wizard shows this instead of raw JSON: the official developer
    console, the exact steps in order, what to copy where, the exact
    redirect URI this server expects (computed from the running server,
    not a hardcoded guess), the scopes requested, and the review/audit
    note. ``credentials[].saved`` are presence flags only — no values.
    """
    _require_platform(platform)  # 404 for an unknown platform
    store = _settings_store()
    return connect_guide(
        platform,
        redirect_uri=redirect_uri(platform),
        client_id_saved=bool(store.get_credential(platform, "client_id")),
        client_secret_saved=bool(store.get_credential(platform, "client_secret")),
    )


@app.get("/onboarding")
def get_onboarding():
    """First-run progress for the onboarding wizard (resumable).

    Stored in the same settings.json as the other preferences, so the
    wizard can resume where the user left off and stops nagging once
    ``completed`` is true.
    """
    return _settings_store().get_onboarding()


@app.put("/onboarding")
def update_onboarding(payload: OnboardingUpdate):
    """Record onboarding progress. Partial: omitted fields are kept."""
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        return _settings_store().update_onboarding(patch)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/auth/{platform}/start")
def start_oauth(platform: str):
    """Return the platform authorize URL for the Connect button to open.

    409 when the platform app credentials are not configured yet — the
    Settings page is where client id/secret are saved first.
    """
    spec = _require_platform(platform)
    store = _settings_store()
    client_id = store.get_credential(platform, "client_id")
    if not client_id:
        raise HTTPException(
            409,
            f"{spec.display_name} is not configured yet — save the app client id "
            "and secret in Settings first (the redirect URI shown there must "
            "also be registered in the platform app).",
        )
    state = _OAUTH_STATES.new(platform)
    return {
        "authorize_url": build_authorize_url(
            platform,
            client_id=client_id,
            redirect_uri=redirect_uri(platform),
            state=state,
        ),
        "redirect_uri": redirect_uri(platform),
        "state": state,
    }


@app.get("/auth/callback/{platform}")
def oauth_callback(
    platform: str,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """Platform redirect target: exchange the code, store the token, done."""
    spec = _require_platform(platform)
    if error:
        return _callback_page(
            title="Connection refused",
            message=f"{spec.display_name} refused the connection ({error}).",
            ok=False,
        )
    if not code or not state:
        return _callback_page(
            title="Connection failed",
            message="The platform redirected back without a code — try Connect again.",
            ok=False,
        )
    store = _settings_store()
    try:
        _OAUTH_STATES.consume(state, platform)
        client_id = store.get_credential(platform, "client_id")
        client_secret = store.get_credential(platform, "client_secret")
        if not client_id or not client_secret:
            raise OAuthError(
                "not_configured",
                f"{spec.display_name} app credentials are missing — save them in Settings first.",
            )
        token = exchange_code(
            platform,
            code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri(platform),
        )
    except OAuthError as exc:
        return _callback_page(title="Connection failed", message=exc.message, ok=False)
    record = TokenRecord.from_token_response(platform, token)
    try:  # profile is best-effort: a token without a name still connects
        profile = fetch_profile(platform, record.access_token)
        record.account_name = profile.get("account_name") or ""
        record.avatar_url = profile.get("avatar_url") or ""
        record.account_ref = profile.get("account_ref") or ""
    except OAuthError:
        pass
    TokenStore(_secrets()).save(record)
    # First-run onboarding: a real connect marks this step done, so the
    # wizard resumes past it instead of nagging on every launch.
    _settings_store().update_onboarding({"first_channel_connected": True})
    account = (
        db.query(SocialAccount)
        .filter_by(provider=platform)
        .order_by(SocialAccount.created_at.desc())
        .first()
    )
    if account is None:
        account = SocialAccount(provider=platform)
        db.add(account)
    account.label = record.account_name or account.label or spec.display_name
    account.account_ref = record.account_ref or account.account_ref
    account.scopes = record.scopes
    account.status = "active"
    account.token_env_var = ""  # OAuth tokens live in the encrypted store now
    db.commit()
    name = record.account_name or spec.display_name
    return _callback_page(
        title=f"{spec.display_name} connected",
        message=f"Connected as {name}.",
        ok=True,
    )


@app.post("/channels/{platform}/refresh")
def refresh_channel(platform: str, db: Session = Depends(get_db)):
    """Refresh (YouTube/TikTok) or extend (Meta, 60-day long-lived) the token."""
    spec = _require_platform(platform)
    secrets = _secrets()
    record = TokenStore(secrets).load(platform)
    if record is None:
        raise HTTPException(404, f"{spec.display_name} is not connected.")
    store = _settings_store()
    try:
        token = refresh_access_token(
            platform,
            client_id=store.get_credential(platform, "client_id") or "",
            client_secret=store.get_credential(platform, "client_secret") or "",
            refresh_token=record.refresh_token,
            access_token=record.access_token,
        )
    except OAuthError as exc:
        raise HTTPException(502, exc.message) from exc
    new_record = TokenRecord.from_token_response(platform, token)
    # Providers often omit the refresh token on refresh — keep the old one.
    new_record.refresh_token = new_record.refresh_token or record.refresh_token
    new_record.account_name = record.account_name
    new_record.avatar_url = record.avatar_url
    new_record.account_ref = record.account_ref
    TokenStore(secrets).save(new_record)
    return channel_status(platform, secrets=secrets, db=db)


@app.post("/channels/{platform}/disconnect")
def disconnect_channel(platform: str, db: Session = Depends(get_db)):
    """Delete the stored token. The account row stays, marked disconnected."""
    _require_platform(platform)
    secrets = _secrets()
    TokenStore(secrets).delete(platform)
    account = (
        db.query(SocialAccount)
        .filter_by(provider=platform)
        .order_by(SocialAccount.created_at.desc())
        .first()
    )
    if account is not None:
        account.status = "disconnected"
        db.commit()
    return channel_status(platform, secrets=secrets, db=db)


@app.get("/settings")
def get_settings():
    """Masked credentials + preferences + environment status. No secret values."""
    return _settings_store().public_view(env_dry_run=settings.dry_run)


@app.put("/settings")
def update_settings(payload: SettingsUpdate):
    """Save preferences / credentials. Returns the masked public view."""
    store = _settings_store()
    current = store.load()
    if payload.dry_run is not None:
        current.dry_run = payload.dry_run
    if payload.default_privacy is not None:
        bad = {
            p: v
            for p, v in payload.default_privacy.items()
            if p not in PLATFORMS or v not in ("public", "private", "unlisted")
        }
        if bad:
            raise HTTPException(422, f"invalid default_privacy entries: {sorted(bad)}")
        current.default_privacy = dict(payload.default_privacy)
    if payload.default_platforms is not None:
        bad = [p for p in payload.default_platforms if p not in PLATFORMS]
        if bad:
            raise HTTPException(422, f"unknown platforms in default_platforms: {bad}")
        current.default_platforms = list(payload.default_platforms)
    if payload.llm_provider is not None:
        current.llm_provider = payload.llm_provider
    if payload.llm_base_url is not None:
        if not payload.llm_base_url.startswith(("http://", "https://")):
            raise HTTPException(422, "llm_base_url must start with http:// or https://")
        current.llm_base_url = payload.llm_base_url.rstrip("/")
    if payload.llm_model is not None:
        current.llm_model = payload.llm_model
    store.save(current)
    if payload.llm_api_key is not None:
        store.set_credential("llm", "api_key", payload.llm_api_key or None)
    if payload.gemini_api_key is not None:
        store.set_credential("gemini", "api_key", payload.gemini_api_key or None)
    if payload.credentials:
        for platform, creds in payload.credentials.items():
            _require_platform(platform)
            for kind in ("client_id", "client_secret"):
                if kind in creds:
                    store.set_credential(platform, kind, creds[kind] or None)
    return store.public_view(env_dry_run=settings.dry_run)


@app.post("/settings/test-connection/{service}")
def test_connection(service: str):
    """Hit the real API with the stored credential; report the exact result.

    On failure the 502 detail is a plain-language message plus the next
    step — never a raw traceback or a bare HTTP code on its own. On
    success the full result (ok / message / code / next_step) is returned.
    """
    if service not in (*PLATFORMS, "llm"):
        raise HTTPException(404, f"unknown service: {service}")
    result = _settings_store().test_connection(service)
    if not result["ok"]:
        detail = result["message"]
        if result.get("next_step"):
            detail = f"{detail} Next step: {result['next_step']}"
        raise HTTPException(502, detail)
    return result


# ------------------------------------------------- one-click FFmpeg install
# One installer per data dir (tests point CLIPFORGE_DATA_DIR at a temp
# dir); the installer owns the background thread + pollable state file.
_INSTALLERS: dict[str, FfmpegInstaller] = {}


def _ffmpeg_installer() -> FfmpegInstaller:
    key = str(_data_dir())
    installer = _INSTALLERS.get(key)
    if installer is None:
        installer = FfmpegInstaller(
            _data_dir(),
            on_installed=lambda path: _settings_store().set_ffmpeg_path(path),
        )
        _INSTALLERS[key] = installer
    return installer


@app.get("/ffmpeg/status")
def ffmpeg_status():
    """Pollable FFmpeg install state + live detection.

    ``state`` is one of idle / downloading / verifying / extracting /
    installing / done / error / unsupported; ``progress_pct`` moves
    0 → 100 through a run; ``error`` is a plain-language message (or
    ""), never a traceback — it starts with the step that failed
    ("Download failed: …" / "Verification failed: …" / "Extraction
    failed: …" / "Install failed: …"), and ``failed_step`` carries that
    step machine-readably (download / verify / extract / install).
    ``path_warning`` is a non-fatal Windows PATH warning on an
    otherwise successful install; ``checksum_verified`` is True when
    the published SHA-256 matched, False when no checksum file was
    available for the build (the download still came over HTTPS from
    the official source), None before verification ran. ``found`` /
    ``path`` / ``version`` say whether FFmpeg is usable right now,
    whatever installed it.
    """
    return _ffmpeg_installer().status()


@app.post("/ffmpeg/install", status_code=202)
def ffmpeg_install(force: bool = Query(default=False)):
    """Start the one-click FFmpeg install (returns immediately).

    Downloads the official build for this platform into
    ``<data_dir>/ffmpeg/``, verifies it, and adds it to the user PATH
    on Windows. Poll ``GET /ffmpeg/status`` for progress. A second
    call while a run is active returns the live status instead of
    starting a duplicate download; ``force=true`` re-installs over an
    existing FFmpeg. macOS answers 422 (not auto-installed yet).
    """
    result = _ffmpeg_installer().start_install(force=force)
    if result["state"] == "unsupported":
        raise HTTPException(422, result["error"] or result["message"])
    return result


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
    if not dry_run and _effective_dry_run():
        raise HTTPException(409, "Live publishing disabled: dry-run is on")
    return {"dry_run": dry_run, "results": run_due_posts(db, dry_run=dry_run)}


@app.get("/scheduler/status")
def get_scheduler_status(db: Session = Depends(get_db)):
    """Scheduler runner state (docs/API-CONTRACT.md §4).

    ``running`` is true only while a run is executing (runs are
    synchronous, so this is almost always false between calls).
    ``last_run_at`` / ``last_error`` come from the latest recorded
    :class:`SchedulerRun`; ``due_now`` counts posts due right now.
    ``next_run_at`` is null: the scheduler only runs on demand
    (``POST /scheduler/run`` / CLI) — no cron schedule is configured
    in this release.
    """
    last = db.query(SchedulerRun).order_by(SchedulerRun.started_at.desc()).first()
    return {
        "running": bool(last and last.status == "running"),
        "last_run_at": ((last.finished_at or last.started_at).isoformat() if last else None),
        "next_run_at": None,
        "due_now": len(due_posts(db)),
        "last_error": last.error if last else None,
        "last_run": (
            {
                "id": last.id,
                "status": last.status,
                "dry_run": last.dry_run,
                "processed": last.processed,
                "failed_count": last.failed_count,
                "duration_s": last.duration_s,
            }
            if last
            else None
        ),
    }


# ------------------------------------------------------------------ worker queue
@app.get("/worker/queue")
def get_worker_queue(db: Session = Depends(get_db)):
    """Background worker state: running jobs, queued jobs, what is next,
    and recent runs with outcome + duration. The queue is the jobs
    table itself (DB-backed, not in-process) — see clipforge.worker."""
    return queue_summary(db)


@app.post("/worker/run-next")
def run_next_job_endpoint(db: Session = Depends(get_db)):
    """Run the oldest queued/pending job now (synchronous).

    Returns the job detail when a job ran, or ``{"ran": null}`` when
    the queue is empty. A missing dependency leaves the job ``blocked``
    with ``blocked_by`` — still a 200, the block is a state.
    """
    result = run_next_queued(db)
    if result is None:
        return {"ran": None, "queue": queue_summary(db)}
    job = _get_or_404(db, Job, result["job_id"], "job")
    return {"ran": _job_out(job, db, detail=True), "queue": queue_summary(db)}


# ------------------------------------------------------------------ automation
class AutomationRuleCreate(BaseModel):
    kind: str
    name: str | None = None
    enabled: bool = True
    config: dict[str, Any] | None = None


class AutomationRuleUpdate(BaseModel):
    enabled: bool | None = None
    name: str | None = None
    config: dict[str, Any] | None = None


@app.get("/automation/feed")
def get_automation_feed(
    kind: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
    clip_candidate_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Automation history: every rule action + skip, newest first.

    Filter by rule ``kind`` (auto_seo_on_approve /
    auto_pick_next_clip / auto_queue_approved), ``job_id`` or
    ``clip_candidate_id``. This is the audit trail that proves what
    the automation did — and what it deliberately did not do.
    """
    query = db.query(AutomationEvent).order_by(AutomationEvent.created_at.desc())
    if kind:
        query = query.filter_by(kind=kind)
    if job_id:
        query = query.filter_by(job_id=job_id)
    if clip_candidate_id:
        query = query.filter_by(clip_candidate_id=clip_candidate_id)
    return [automation.event_out(e) for e in query.limit(limit).all()]


@app.get("/automation/rules")
def list_automation_rules(db: Session = Depends(get_db)):
    """All automation rules, contract shape (docs/API-CONTRACT.md §1):
    ``{rules: [{key, label, enabled, description, ...}]}``. ``key`` is
    the frontend toggle key (``retention_feedback``, ``auto_seo``,
    ``staggered_schedule``, ``auto_approve_high_score``); ``kind`` is
    the backend rule name. The Automation page's toggles map 1:1."""
    return {"rules": [automation.rule_out(r) for r in automation.ensure_default_rules(db)]}


class AutomationRuleToggle(BaseModel):
    key: str  # frontend key (retention_feedback, ...) or backend kind
    enabled: bool


@app.put("/automation/rules")
def set_automation_rule(payload: AutomationRuleToggle, db: Session = Depends(get_db)):
    """Set one rule on/off by its frontend ``key`` (or backend kind).

    Returns the updated rule in the same shape as GET. ``409`` when
    the key is server-locked: ``dry_run_guard`` is enforced by the
    server setting (``CLIPFORGE_DRY_RUN``), never by a toggle.
    Unknown keys are 422.
    """
    if payload.key == "dry_run_guard":
        raise HTTPException(
            409,
            "dry_run_guard is server-locked: live publishing is controlled "
            "by the CLIPFORGE_DRY_RUN server setting, not by a toggle.",
        )
    kind = automation.resolve_kind(payload.key)
    if kind is None:
        raise HTTPException(422, f"unknown automation rule key {payload.key!r}")
    rule = automation.get_rule(db, kind)
    if rule is None:
        automation.ensure_default_rules(db)
        rule = automation.get_rule(db, kind)
    assert rule is not None
    rule.enabled = payload.enabled
    db.commit()
    return automation.rule_out(rule)


@app.post("/automation/rules", status_code=201)
def upsert_automation_rule(payload: AutomationRuleCreate, db: Session = Depends(get_db)):
    """Create or update a rule by ``kind`` (unknown kinds are 422).

    ``kind`` accepts a backend kind (``auto_pick_next_clip``, ...) or
    a frontend key (``retention_feedback``, ...). Prefer
    ``PUT /automation/rules`` for simple on/off toggles.
    """
    kind = automation.resolve_kind(payload.kind)
    if kind is None:
        raise HTTPException(422, f"unknown automation rule kind {payload.kind!r}")
    known = {spec["kind"]: spec for spec in automation.DEFAULT_RULES}
    rule = automation.get_rule(db, kind)
    if rule is None:
        rule = AutomationRule(
            name=payload.name or known[kind]["name"],
            kind=kind,
            enabled=payload.enabled,
            config_json=payload.config or known[kind]["config"],
        )
        db.add(rule)
    else:
        if payload.name is not None:
            rule.name = payload.name
        rule.enabled = payload.enabled
        if payload.config is not None:
            rule.config_json = payload.config
    db.commit()
    return automation.rule_out(rule)


@app.patch("/automation/rules/{rule_id}")
def update_automation_rule(
    rule_id: str, payload: AutomationRuleUpdate, db: Session = Depends(get_db)
):
    rule = _get_or_404(db, AutomationRule, rule_id, "automation rule")
    if payload.enabled is not None:
        rule.enabled = payload.enabled
    if payload.name is not None:
        rule.name = payload.name
    if payload.config is not None:
        rule.config_json = payload.config
    db.commit()
    return automation.rule_out(rule)


@app.post("/automation/run")
def run_automation(db: Session = Depends(get_db)):
    """One automation pass: auto-queue approved clips with no posts yet
    (per platform caps) and pick the next clip for jobs in review.

    Only creates ``pending_approval`` posts — nothing publishes.
    Returns a summary; every action is also in /automation/feed.
    """
    return automation.run_automation_pass(db)


@app.post("/automation/pick-next/{job_id}")
def pick_next_clip_endpoint(job_id: str, db: Session = Depends(get_db)):
    """Recommend the next clip for a job from retention feedback.

    Returns the pick (adjusted score, band, adjustment reason) or
    ``{"pick": null, "reason": ...}`` when nothing qualifies. The pick
    is a recommendation only — it never approves a candidate.
    """
    _get_or_404(db, Job, job_id, "job")
    pick = automation.pick_next_clip(db, job_id)
    if pick is None:
        return {
            "job_id": job_id,
            "pick": None,
            "reason": "no pending candidate qualifies (see /automation/feed)",
        }
    return {"job_id": job_id, "pick": pick}


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


# ============================================================ v0.5.0 ===
# ClipForge Agent + Gemini provider (worker 2 of 3, v0.5.0).
# One clearly-marked block: agent status/config/start/stop and the
# Gemini LLM endpoints. All errors are plain language — never a
# traceback, never a secret value in any response.
#
# Rights guardrail (applies to every string in this block): the agent
# is for the user's OWN videos or videos they have rights / a licence
# to use. It is not a tool to re-upload other creators' videos.
# ============================================================ v0.5.0 ===


class AgentConfigUpdate(BaseModel):
    """Partial agent config update. Omitted = keep; see AgentConfig."""

    enabled: bool | None = None
    source_type: str | None = None
    source_url: str | None = None
    niche: str | None = None
    auto_post: bool | None = None
    rights_confirmed: bool | None = None
    transcript_text: str | None = None
    top_n: int | None = None
    platforms: list[str] | None = None


class GeminiKeyIn(BaseModel):
    api_key: str = ""
    model: str | None = None


def _agent() -> agent_mod.Agent:
    """The one shared agent for this data dir (start/stop share a thread)."""
    return agent_mod.get_agent(_data_dir(), _Session, _settings_store())


def _agent_config_out(config: agent_mod.AgentConfig) -> dict:
    """GET/PUT /api/agent/config shape (contract fields + extras)."""
    return {
        "enabled": config.enabled,
        "source_type": config.source_type,
        "source_url": config.source_url,
        "niche": config.niche,
        "auto_post": config.auto_post,
        "rights_confirmed": config.rights_confirmed,
        "transcript_text": config.transcript_text,
        "top_n": config.top_n,
        "platforms": list(config.platforms),
        "rights_notice": agent_mod.RIGHTS_NOTICE,
        "niches": list(PRESET_NAMES),
    }


@app.get("/api/agent/status")
def agent_status(db: Session = Depends(get_db)):
    """Agent status + per-account roll-up (queued / approved / published)."""
    agent = _agent()
    status = agent.status()
    status["accounts"] = agent_mod.accounts_summary(db, status)
    return status


@app.get("/api/agent/config")
def get_agent_config():
    """Current agent config + the rights notice + the niche presets."""
    return _agent_config_out(_agent().store.load_config())


@app.put("/api/agent/config")
def update_agent_config(payload: AgentConfigUpdate):
    """Save agent config (partial). Structural problems are 422 in
    plain language. The rights flag is NOT required to *save* — it is
    required to *run* (POST /api/agent/start enforces it)."""
    agent = _agent()
    config = agent.store.load_config()
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    for key, value in patch.items():
        setattr(config, key, value)
    if "niche" in patch:
        from clipforge.niches import normalize_niche  # noqa: PLC0415

        config.niche = normalize_niche(config.niche)
    try:
        config.validate_structure()
    except agent_mod.AgentError as exc:
        raise HTTPException(422, str(exc)) from exc
    agent.store.save_config(config)
    return _agent_config_out(config)


@app.post("/api/agent/start")
def start_agent():
    """Start the agent (background thread). 409 when already running,
    422 when it is off / has no source / rights are not confirmed."""
    agent = _agent()
    try:
        state = agent.start()
    except agent_mod.AgentError as exc:
        message = str(exc)
        status_code = 409 if "already running" in message else 422
        raise HTTPException(status_code, message) from exc
    return {"ok": True, "state": state}


@app.post("/api/agent/stop")
def stop_agent():
    """Ask the agent to stop. Always 200 with the current state."""
    return {"ok": True, "state": _agent().stop()}


@app.get("/api/llm/providers")
def list_llm_providers():
    """LLM providers with configured status (masked, last4 only).

    ``active`` marks the engine the agent would use right now:
    Gemini when its key is saved, then OpenAI-compatible, else the
    rule engine (always available, no key needed).
    """
    store = _settings_store()
    engine, _provider = resolve_engine(store)
    gemini_view = store.credential_view("gemini", "api_key")
    openai_view = store.credential_view("llm", "api_key")
    settings_now = store.load()
    return [
        {
            "id": "gemini",
            "name": "Gemini",
            "model": settings_now.gemini_model or GEMINI_DEFAULT_MODEL,
            "models": list(GEMINI_MODELS),
            "configured": gemini_view["configured"],
            "last4": gemini_view["last4"],
            "masked": gemini_view["masked"],
            "source": gemini_view["source"],
            "active": engine == "gemini",
        },
        {
            "id": "openai",
            "name": "OpenAI-compatible",
            "model": settings_now.llm_model,
            "base_url": settings_now.llm_base_url,
            "configured": openai_view["configured"],
            "last4": openai_view["last4"],
            "masked": openai_view["masked"],
            "source": openai_view["source"],
            "active": engine == "openai",
        },
        {
            "id": "rules",
            "name": "Rule-based (no key needed)",
            "model": "",
            "configured": True,
            "last4": "",
            "masked": "",
            "source": "builtin",
            "active": engine == "rules",
        },
    ]


@app.post("/api/llm/gemini/test")
def test_gemini_key(payload: GeminiKeyIn):
    """Test a Gemini key against the real API (plain-language result).

    ``api_key`` may be omitted to test the *saved* key. The key is
    never echoed back, logged, or included in any message.
    """
    model = payload.model or GEMINI_DEFAULT_MODEL
    if model not in GEMINI_MODELS:
        raise HTTPException(
            422, f"Unknown Gemini model {model!r} — pick one of {list(GEMINI_MODELS)}."
        )
    key = (payload.api_key or "").strip()
    if not key:
        key = _settings_store().get_credential("gemini", "api_key") or ""
    if not key:
        raise HTTPException(
            422,
            "No Gemini API key given and none saved — paste your key "
            "(Google AI Studio > Get API key) and test again.",
        )
    # A test must never 500: any unexpected failure (e.g. a broken
    # proxy config on the machine) comes back as a plain-language
    # {ok: False} so the Test button can show it.
    try:
        result = test_gemini(key, model)
    except Exception as exc:  # noqa: BLE001 — test endpoint never 500s
        return {
            "ok": False,
            "model": model,
            "message": (
                "Gemini could not be tested right now — "
                f"check your internet connection and try again. ({exc})"
            ),
        }
    return {"ok": result["ok"], "model": result["model"], "message": result["message"]}


@app.post("/api/llm/gemini/save")
def save_gemini_key(payload: GeminiKeyIn):
    """Save (or clear, with "") a Gemini key in the OS keyring.

    The response carries only the masked view — the key itself is
    never returned. An optional ``model`` is saved alongside it.
    """
    store = _settings_store()
    key = (payload.api_key or "").strip()
    if payload.model:
        if payload.model not in GEMINI_MODELS:
            raise HTTPException(
                422,
                f"Unknown Gemini model {payload.model!r} — pick one of {list(GEMINI_MODELS)}.",
            )
        current = store.load()
        current.gemini_model = payload.model
        store.save(current)
    if key:
        store.set_credential("gemini", "api_key", key)
    else:
        store.set_credential("gemini", "api_key", None)
    view = store.credential_view("gemini", "api_key")
    return {
        "ok": True,
        "configured": view["configured"],
        "last4": view["last4"],
        "masked": view["masked"],
        "message": (
            "Gemini API key saved."
            if view["configured"]
            else "Gemini API key cleared — the agent will use the rule engine."
        ),
    }


# ============================================================ / v0.5.0 ===


# A previous one-click install already put FFmpeg in
# <data_dir>/ffmpeg/bin: prefer it from the first request, with no
# restart and no PATH edit (see clipforge.ffmpeg_install).
ensure_bundled_on_path(_data_dir())


__all__ = ["app"]
