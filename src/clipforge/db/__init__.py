"""Database layer for ClipForge v0.1.0 — SQLAlchemy models + session helpers.

SQLite by default (``DATABASE_URL`` env, default ``sqlite:///./clipforge.db``);
Postgres works unchanged via the same models (e.g.
``postgresql+psycopg2://user:pass@host/db``).

Status enums (per spec):
- Job: pending / transcribing / scoring / rendering / review / ready / failed
- ClipCandidate: pending / approved / rejected
- Post: draft / pending_approval / approved / scheduled / publishing /
  published / failed / dry_run

Tokens are NEVER stored here: :class:`SocialAccount` stores only the *name*
of the env var holding the token (``token_env_var``), never the value.
"""

from __future__ import annotations

import enum
import os
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


class JobStatus(enum.StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    TRANSCRIBING = "transcribing"
    SCORING = "scoring"
    RENDERING = "rendering"
    REVIEW = "review"
    READY = "ready"
    BLOCKED = "blocked"  # waiting on a missing dependency (tool/key) — see workflow.py
    CANCELLED = "cancelled"
    FAILED = "failed"


class CandidateStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PostStatus(enum.StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    DRY_RUN = "dry_run"


class Platform(enum.StrEnum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    TIKTOK = "tiktok"


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    source_type: Mapped[str] = mapped_column(String(16), default="local")  # local|youtube
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    own_content: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.PENDING.value)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_json: Mapped[dict] = mapped_column("meta", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    candidates: Mapped[list[ClipCandidate]] = relationship(
        "ClipCandidate", back_populates="job", cascade="all, delete-orphan"
    )


class ClipCandidate(Base):
    __tablename__ = "clip_candidates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("jobs.id"), index=True)
    n: Mapped[int] = mapped_column(Integer, default=0)  # candidate number within job
    start_s: Mapped[float] = mapped_column(Float, default=0.0)
    end_s: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    hook_rating: Mapped[float] = mapped_column(Float, default=0.0)
    hook_line: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    transcript_excerpt: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=CandidateStatus.PENDING.value)
    trim_start_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    trim_end_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    job: Mapped[Job] = relationship("Job", back_populates="candidates")

    @property
    def duration_s(self) -> float:
        start = self.trim_start_s if self.trim_start_s is not None else self.start_s
        end = self.trim_end_s if self.trim_end_s is not None else self.end_s
        return max(0.0, end - start)


class BrandKit(Base):
    """Brand kit: colours/fonts/logo/caption style/intro-outro for a channel.

    Applied at render/SEO time (see :mod:`clipforge.brandkits`). A kit
    never changes the clip itself — it styles captions/SEO copy and the
    optional intro/outro text.
    """

    __tablename__ = "brand_kits"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(120))
    primary_colour: Mapped[str] = mapped_column(String(7), default="#FFFFFF")
    secondary_colour: Mapped[str] = mapped_column(String(7), default="#000000")
    accent_colour: Mapped[str] = mapped_column(String(7), default="#FFD700")
    font_name: Mapped[str] = mapped_column(String(120), default="DejaVu Sans")
    caption_style: Mapped[str] = mapped_column(String(30), default="bold-pop")
    logo_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    intro_text: Mapped[str] = mapped_column(Text, default="")
    outro_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class TitleVariant(Base):
    """One A/B title variant for a clip on one platform.

    ``is_control`` marks the baseline title. Outcomes (views /
    completion / viewed%) are recorded on the variant when analytics
    arrive — see :mod:`clipforge.abtest`. A scheduled post references
    its assigned variant via :attr:`Post.title_variant_id`.
    """

    __tablename__ = "title_variants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    clip_candidate_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("clip_candidates.id"), index=True
    )
    platform: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(Text)
    is_control: Mapped[bool] = mapped_column(Boolean, default=False)
    views: Mapped[int] = mapped_column(Integer, default=0)
    avg_view_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    completion_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    viewed_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    outcome_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RetentionPoint(Base):
    """One retention observation for a published clip.

    Ingested manually (or from a platform export) until platform
    analytics keys exist — see :mod:`clipforge.retention`. ``source``
    records where the numbers came from (``manual`` / ``youtube`` /
    ``meta`` / ``tiktok``); live platform feeds are NOT wired in this
    release, so ``source`` is ``manual`` unless a feed exists.
    """

    __tablename__ = "retention_points"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    post_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("posts.id"), nullable=True, index=True
    )
    clip_candidate_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("clip_candidates.id"), nullable=True, index=True
    )
    platform: Mapped[str] = mapped_column(String(20))
    clip_duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    avg_view_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    completion_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    viewed_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    dropoff_second: Mapped[float | None] = mapped_column(Float, nullable=True)
    views: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BrollAsset(Base):
    """B-roll attached to a clip — licensed/source-only (see :mod:`clipforge.broll`).

    ``source_type`` is ``source_video`` (cut from the clip's own source
    video) or ``uploaded_licensed`` (a file the user uploaded and
    declared licensed). URLs / third-party downloads are rejected at
    validation time and never stored. ``declaration`` is the user's
    source/rights statement for this asset.
    """

    __tablename__ = "broll_assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    clip_candidate_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("clip_candidates.id"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(20))  # source_video|uploaded_licensed
    source_path: Mapped[str] = mapped_column(Text)
    source_start_s: Mapped[float] = mapped_column(Float, default=0.0)
    source_end_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    clip_start_s: Mapped[float] = mapped_column(Float, default=0.0)  # overlay start in clip
    clip_end_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    position: Mapped[str] = mapped_column(String(30), default="picture-in-picture")
    width_frac: Mapped[float] = mapped_column(Float, default=0.45)
    declaration: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SocialAccount(Base):
    __tablename__ = "social_accounts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    provider: Mapped[str] = mapped_column(String(20))  # youtube|instagram|facebook|tiktok
    label: Mapped[str] = mapped_column(String(200), default="")
    account_ref: Mapped[str] = mapped_column(String(200), default="")  # handle / page id / channel
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    # NAME of the env var holding the token — never the token itself.
    token_env_var: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    daily_cap_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    posts: Mapped[list[Post]] = relationship("Post", back_populates="account")


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_post_idempotency"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    clip_candidate_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("clip_candidates.id"), nullable=True
    )
    account_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("social_accounts.id"), nullable=True
    )
    platform: Mapped[str] = mapped_column(String(20))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=PostStatus.PENDING_APPROVAL.value)
    idempotency_key: Mapped[str] = mapped_column(
        String(64), default=lambda: uuid.uuid4().hex, unique=True
    )
    seo_json: Mapped[dict] = mapped_column("seo", JSON, default=dict)
    video_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_variant_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("title_variants.id"), nullable=True
    )
    brand_kit_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("brand_kits.id"), nullable=True
    )
    publish_result_json: Mapped[dict | None] = mapped_column("publish_result", JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    account: Mapped[SocialAccount | None] = relationship("SocialAccount", back_populates="posts")
    candidate: Mapped[ClipCandidate | None] = relationship("ClipCandidate")


class AnalyticsSnapshot(Base):
    __tablename__ = "analytics_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    account_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("social_accounts.id"), nullable=True
    )
    platform: Mapped[str] = mapped_column(String(20))
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    views: Mapped[int] = mapped_column(Integer, default=0)
    followers: Mapped[int] = mapped_column(Integer, default=0)  # subs on YouTube
    watch_hours: Mapped[float] = mapped_column(Float, default=0.0)
    shorts_views_90d: Mapped[int] = mapped_column(Integer, default=0)
    completion_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    viewed_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_json: Mapped[dict] = mapped_column("raw", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MonetizationProgress(Base):
    """Cached monetization-tracker snapshot (computed by monetization.progress)."""

    __tablename__ = "monetization_progress"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    account_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("social_accounts.id"), nullable=True
    )
    platform: Mapped[str] = mapped_column(String(20))
    program: Mapped[str] = mapped_column(String(120))
    progress_pct: Mapped[float] = mapped_column(Float, default=0.0)
    current_json: Mapped[dict] = mapped_column("current", JSON, default=dict)
    threshold_json: Mapped[dict] = mapped_column("threshold", JSON, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class JobStageEvent(Base):
    """One stage transition of one job — the live-workflow audit trail.

    Every stage (ingest / transcribe / score / render / seo / review)
    writes a ``started`` row and then a terminal row (``done`` /
    ``failed`` / ``blocked`` / ``skipped``) with progress %, elapsed
    seconds and a JSON ``detail`` (engine used, counts, alternates,
    missing-dependency code, ...). The frontend polls
    ``GET /jobs/{id}`` and renders this as the job's stage history.
    """

    __tablename__ = "job_stage_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("jobs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(30))  # see workflow.STAGES
    status: Mapped[str] = mapped_column(
        String(20), default="started"
    )  # started|done|failed|blocked|skipped
    progress_pct: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    detail_json: Mapped[dict] = mapped_column("detail", JSON, default=dict)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WorkerRun(Base):
    """One worker execution of one job — what ran, outcome, duration.

    The queue view (``GET /worker/queue``) reads the currently
    ``running`` rows plus recent finished rows. A run is created when
    the worker starts a job and closed when it finishes / fails /
    blocks, so a crashed worker leaves a visible orphan ``running`` row
    instead of silent nothing.
    """

    __tablename__ = "worker_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("jobs.id"), index=True)
    status: Mapped[str] = mapped_column(
        String(20), default="running"
    )  # running|done|failed|blocked|cancelled
    outcome: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SchedulerRun(Base):
    """One scheduler execution — what ``GET /scheduler/status`` reads.

    Recorded by :func:`clipforge.scheduler.run_due_posts` (the single
    entry point used by the API endpoint and the CLI), so the status
    endpoint reports real runs instead of inferring from posts. A run
    left in ``running`` means the process died mid-run. ``next_run_at``
    is intentionally not stored here: the scheduler only runs on
    demand (``POST /scheduler/run`` / CLI) — there is no cron schedule
    in this release, so the status endpoint reports ``next_run_at:
    null`` until one is configured.
    """

    __tablename__ = "scheduler_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    status: Mapped[str] = mapped_column(String(20), default="running")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AutomationEvent(Base):
    """One logged automation action — the automation feed/history.

    Every rule-driven action (SEO auto-generated, clip auto-picked,
    post auto-queued, rule skipped because of a cap / missing account)
    writes a row here with ``actor="automation"``. Human actions are
    never written here; they are visible in the normal API responses.
    """

    __tablename__ = "automation_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    kind: Mapped[str] = mapped_column(String(40))  # rule kind, e.g. auto_queue_approved
    action: Mapped[str] = mapped_column(String(80))
    actor: Mapped[str] = mapped_column(String(20), default="automation")
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    clip_candidate_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    post_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    detail_json: Mapped[dict] = mapped_column("detail", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AutomationRule(Base):
    """One toggleable automation rule (see :mod:`clipforge.automation`).

    Rules are seeded with defaults on first use; the API can enable /
    disable them and edit their JSON ``config``. A disabled rule never
    fires; every firing is logged to :class:`AutomationEvent`.
    """

    __tablename__ = "automation_rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(40), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config_json: Mapped[dict] = mapped_column("config", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class CandidateSeo(Base):
    """Stored per-platform SEO variant for one clip candidate.

    Generated deterministically by :mod:`clipforge.seo` (template, not
    LLM, unless ``CLIPFORGE_LLM_API_KEY`` is set and polish is wired).
    Written by the worker at the ``seo`` stage and by the automation
    rule ``auto_seo_on_approve``; the review UI reads these instead of
    re-generating SEO on every render. One row per (candidate, platform).
    """

    __tablename__ = "candidate_seo"
    __table_args__ = (UniqueConstraint("clip_candidate_id", "platform", name="uq_candidate_seo"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    clip_candidate_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("clip_candidates.id"), index=True
    )
    platform: Mapped[str] = mapped_column(String(20))
    seo_json: Mapped[dict] = mapped_column("seo", JSON, default=dict)
    generated_by: Mapped[str] = mapped_column(String(20), default="worker")  # worker|automation
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


def database_url() -> str:
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("CLIPFORGE_DATABASE_URL")
        or "sqlite:///./clipforge.db"
    )


def make_engine(url: str | None = None):
    url = url or database_url()
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def init_db(url: str | None = None):
    """Create all tables (idempotent). Returns the engine."""
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    return engine


def session_factory(url: str | None = None):
    return sessionmaker(bind=make_engine(url), class_=Session, expire_on_commit=False)


def get_session(url: str | None = None) -> Session:
    """One session (caller closes it). Prefer the FastAPI dependency."""
    return session_factory(url)()


__all__ = [
    "Base",
    "Job",
    "ClipCandidate",
    "Post",
    "SocialAccount",
    "AnalyticsSnapshot",
    "MonetizationProgress",
    "BrandKit",
    "TitleVariant",
    "RetentionPoint",
    "BrollAsset",
    "JobStageEvent",
    "WorkerRun",
    "SchedulerRun",
    "AutomationEvent",
    "AutomationRule",
    "CandidateSeo",
    "JobStatus",
    "CandidateStatus",
    "PostStatus",
    "Platform",
    "init_db",
    "make_engine",
    "session_factory",
    "get_session",
    "database_url",
]
