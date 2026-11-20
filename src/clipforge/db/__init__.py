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
    TRANSCRIBING = "transcribing"
    SCORING = "scoring"
    RENDERING = "rendering"
    REVIEW = "review"
    READY = "ready"
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
