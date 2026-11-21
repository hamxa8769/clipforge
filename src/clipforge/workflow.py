"""Live workflow for ClipForge v0.4.0 — job stages, dependencies, progress.

This module is the shared core behind the worker (``clipforge.worker``)
and the API workflow endpoints. It does not run the pipeline itself;
it defines *how a job is observed*:

* :data:`STAGES` — the visible pipeline: ingest → transcribe → score →
  render → seo → review. Each stage has a weight (share of total
  progress) and the :class:`JobStatus` the job carries while in it.
* :class:`MissingDependencyError` — the structured error returned
  whenever a step cannot run because a tool / key / package is absent.
  The job goes to ``blocked`` (not ``failed``) with the exact missing
  thing named, so the UI can say "install FFmpeg" instead of "error".
* :func:`check_dependencies` — the ``autoclip doctor`` / ShortsFlow
  ``doctor`` equivalent (see docs/REUSE.md): one call that lists every
  tool, package, key and flag the pipeline may need, with availability.
  No secret *value* is ever returned — only presence.
* :class:`StageTracker` — writes :class:`JobStageEvent` rows and keeps
  the job's ``meta`` progress fields (``stage`` / ``progress_pct`` /
  ``eta_s`` / ``demo`` / ``blocked_by``) current, so ``GET /jobs/{id}``
  can render the live workflow without a second query.
* :func:`score_band` — ShortsFlow-style human label for a 0–100 score
  (strong / good / okay / weak) shown next to the number in review.
* :data:`MIN_SCORE_THRESHOLD` — autoclip-style score filter: candidates
  below it are kept as *alternates* (backups you can generate on
  demand) instead of being silently dropped.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from clipforge.db import Job, JobStageEvent, JobStatus, WorkerRun
from clipforge.ytdlp_install import bundled_yt_dlp_path, ytdlp_missing_message

# ---------------------------------------------------------------- stages
# (name, weight % of total progress, JobStatus while the stage runs)
STAGES: list[tuple[str, float, str]] = [
    ("ingest", 10.0, JobStatus.PENDING.value),
    ("transcribe", 30.0, JobStatus.TRANSCRIBING.value),
    ("score", 15.0, JobStatus.SCORING.value),
    ("render", 35.0, JobStatus.RENDERING.value),
    ("seo", 5.0, JobStatus.REVIEW.value),
    ("review", 5.0, JobStatus.REVIEW.value),
]
STAGE_NAMES = [name for name, _, _ in STAGES]
_STAGE_WEIGHT = {name: weight for name, weight, _ in STAGES}
_STAGE_STATUS = {name: status for name, _, status in STAGES}

# Candidates below this score are alternates, not review candidates
# (zhouxiaoka/autoclip: "up to 10 qualifying clips, the rest as backups").
MIN_SCORE_THRESHOLD = 50.0


def stage_progress_before(stage: str) -> float:
    """Progress % already earned when ``stage`` starts."""
    total = 0.0
    for name, weight, _ in STAGES:
        if name == stage:
            return total
        total += weight
    return total


# ---------------------------------------------------------------- errors
# The one download URL every FFmpeg-missing message points at, so a job
# blocked on a missing tool always tells the user exactly where to get it.
FFMPEG_DOWNLOAD_URL = "https://ffmpeg.org/download.html"


def ffmpeg_missing_message(tool: str = "ffmpeg") -> str:
    """Plain-language error for a missing FFmpeg/ffprobe binary.

    Used by ingest / transcribe / render and by the worker's blocked-job
    records, so the message is identical everywhere: no traceback, no
    bare ``FileNotFoundError``, just what is missing and where to get it.
    """
    if tool == "ffprobe":
        return (
            "ffprobe is missing — it ships with FFmpeg. Press 'Install FFmpeg' "
            "in ClipForge (welcome screen or Settings) to install it automatically, "
            f"or get it from {FFMPEG_DOWNLOAD_URL} and make sure it is on PATH."
        )
    return (
        "FFmpeg is missing — press 'Install FFmpeg' in ClipForge (welcome screen "
        "or Settings) to install it automatically, or install it from "
        f"{FFMPEG_DOWNLOAD_URL} and make sure it is on PATH."
    )


class MissingDependencyError(ValueError):
    """A pipeline step cannot run: a tool, key, package or account is missing.

    Jobs blocked by this error go to status ``blocked`` (retryable once
    the dependency exists) — never ``failed``, and never a faked success.
    """

    def __init__(
        self,
        code: str,
        dependency: str,
        kind: str,
        detail: str,
        required_action: str,
    ) -> None:
        self.code = code  # e.g. "ffmpeg_missing", "openai_api_key_missing"
        self.dependency = dependency  # human name, e.g. "FFmpeg"
        self.kind = kind  # tool | api_key | package | account | flag
        self.detail = detail
        self.required_action = required_action
        super().__init__(f"{dependency} missing ({code}): {detail}")

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "dependency": self.dependency,
            "kind": self.kind,
            "detail": self.detail,
            "required_action": self.required_action,
        }


# ---------------------------------------------------------------- doctor
@dataclass
class Dependency:
    """One thing the pipeline may need. ``available`` is presence only."""

    name: str
    kind: str  # tool | package | api_key | flag
    available: bool
    detail: str
    env_var: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "available": self.available,
            "detail": self.detail,
            "env_var": self.env_var,
        }


def check_dependencies() -> list[Dependency]:
    """Check every tool / package / key the pipeline may need.

    Presence only — key *values* are never read or returned. This is
    the ``doctor`` command other clippers ship (artbyjazi/autoclip,
    ghost1412/shortsflow); the API exposes it at ``GET /dependencies``.
    """
    out: list[Dependency] = []

    def _tool(name: str, detail: str) -> Dependency:
        # ffmpeg/ffprobe also count when the one-click installer put them
        # in <data_dir>/ffmpeg/bin (no PATH edit / restart needed there);
        # yt-dlp likewise counts when its installer put it in
        # <data_dir>/yt-dlp.
        found = shutil.which(name) is not None
        if not found and name in ("ffmpeg", "ffprobe"):
            from clipforge.ffmpeg_install import (  # noqa: PLC0415
                bundled_ffmpeg_path,
                bundled_ffprobe_path,
            )

            data_dir = os.environ.get("CLIPFORGE_DATA_DIR") or "./data"
            bundled = (
                bundled_ffmpeg_path(data_dir)
                if name == "ffmpeg"
                else bundled_ffprobe_path(data_dir)
            )
            found = bundled.is_file()
        if not found and name == "yt-dlp":
            data_dir = os.environ.get("CLIPFORGE_DATA_DIR") or "./data"
            found = bundled_yt_dlp_path(data_dir).is_file()
        return Dependency(
            name=name,
            kind="tool",
            available=found,
            detail=detail if found else f"{name} not found on PATH — {detail}",
        )

    out.append(_tool("ffmpeg", "renders clips, extracts audio, burns captions"))
    out.append(_tool("ffprobe", "validates sources (duration / codec / size)"))
    out.append(_tool("yt-dlp", "downloads own-content YouTube sources"))

    def _package(name: str, detail: str) -> Dependency:
        found = importlib.util.find_spec(name) is not None
        return Dependency(
            name=name,
            kind="package",
            available=found,
            detail=detail if found else f"Python package {name} not installed — {detail}",
        )

    out.append(_package("faster_whisper", "local Whisper transcription"))
    out.append(_package("cv2", "OpenCV face/person tracking for the 9:16 reframe"))

    def _key(env_var: str, detail: str) -> Dependency:
        present = bool(os.environ.get(env_var))
        return Dependency(
            name=env_var,
            kind="api_key",
            available=present,
            detail=detail if present else f"env var {env_var} is not set — {detail}",
            env_var=env_var,
        )

    out.append(_key("OPENAI_API_KEY", "Whisper API + LLM highlight/SEO calls"))
    out.append(_key("CLIPFORGE_LLM_API_KEY", "optional LLM polish of SEO copy"))
    out.append(_key("YOUTUBE_REFRESH_TOKEN", "live YouTube uploads"))
    out.append(_key("META_ACCESS_TOKEN", "live Instagram / Facebook publishing"))
    out.append(_key("TIKTOK_ACCESS_TOKEN", "live TikTok publishing"))
    out.append(
        Dependency(
            name="TIKTOK_DIRECT_POST_OPT_IN",
            kind="flag",
            available=os.environ.get("TIKTOK_DIRECT_POST_OPT_IN", "").lower()
            in {"1", "true", "yes", "on"},
            detail="Direct Post stays gated until the TikTok audit passes; "
            "inbox drafts are the safe default until then.",
            env_var="TIKTOK_DIRECT_POST_OPT_IN",
        )
    )
    return out


def require_tool(name: str, detail: str, required_action: str) -> None:
    found = shutil.which(name) is not None
    if not found and name in ("ffmpeg", "ffprobe"):
        from clipforge.ffmpeg_install import (  # noqa: PLC0415
            bundled_ffmpeg_path,
            bundled_ffprobe_path,
        )

        data_dir = os.environ.get("CLIPFORGE_DATA_DIR") or "./data"
        bundled = (
            bundled_ffmpeg_path(data_dir) if name == "ffmpeg" else bundled_ffprobe_path(data_dir)
        )
        found = bundled.is_file()
    if not found and name == "yt-dlp":
        data_dir = os.environ.get("CLIPFORGE_DATA_DIR") or "./data"
        found = bundled_yt_dlp_path(data_dir).is_file()
    if not found:
        raise MissingDependencyError(
            code=f"{name.replace('-', '_')}_missing",
            dependency=name,
            kind="tool",
            detail=detail,
            required_action=required_action,
        )


def require_transcription_backend() -> None:
    """Raise if no transcription backend exists at all.

    faster-whisper (local) wins, else the Whisper API key, else the
    caller must supply ``transcript_text`` for the deterministic
    fallback — which is then flagged ``demo`` on the job, never passed
    off as real transcription.
    """
    if importlib.util.find_spec("faster_whisper") is not None:
        return
    if os.environ.get("OPENAI_API_KEY"):
        return
    raise MissingDependencyError(
        code="transcription_backend_missing",
        dependency="faster-whisper or OPENAI_API_KEY",
        kind="package",
        detail="faster-whisper is not installed and OPENAI_API_KEY is not set, "
        "so there is no transcription backend. Note: the first real "
        "transcription after installing faster-whisper downloads the "
        "Whisper model (about 500 MB for the default 'small' model) from "
        "HuggingFace with no progress shown — set OPENAI_API_KEY instead "
        "to use the Whisper API and skip that download.",
        required_action="Install faster-whisper (pip install faster-whisper) "
        "or set OPENAI_API_KEY — or supply transcript_text to run the "
        "deterministic fallback (flagged demo, not real transcription).",
    )


# ---------------------------------------------------------------- score bands
def score_band(score: float) -> str:
    """Human label for a 0–100 candidate score (ShortsFlow-style)."""
    if score >= 80:
        return "strong"
    if score >= 65:
        return "good"
    if score >= 50:
        return "okay"
    return "weak"


# ---------------------------------------------------------------- stage tracking
def _utcnow() -> datetime:
    return datetime.now(UTC)


def _job_meta(job: Job) -> dict:
    return dict(job.meta_json or {})


def _set_job_meta(session: Session, job: Job, **updates: object) -> None:
    meta = _job_meta(job)
    meta.update(updates)
    job.meta_json = meta
    session.commit()


@dataclass
class StageTracker:
    """Records stage events for one job and keeps its progress current."""

    session: Session
    job: Job
    data_dir: Path | None = None
    _started: dict[str, float] = field(default_factory=dict)

    def begin(self, stage: str, message: str = "") -> None:
        """Open a stage: write a ``started`` event, set job status/progress."""
        self._started[stage] = time.time()
        self.session.add(
            JobStageEvent(
                job_id=self.job.id,
                stage=stage,
                status="started",
                progress_pct=stage_progress_before(stage),
                message=message,
            )
        )
        _set_job_meta(
            self.session,
            self.job,
            stage=stage,
            progress_pct=stage_progress_before(stage),
        )
        self.job.status = _STAGE_STATUS.get(stage, self.job.status)
        self.session.commit()

    def finish(self, stage: str, message: str = "", detail: dict | None = None) -> None:
        """Close a stage as ``done`` with its elapsed time."""
        duration = time.time() - self._started.pop(stage, time.time())
        done_pct = stage_progress_before(stage) + _STAGE_WEIGHT.get(stage, 0.0)
        self.session.add(
            JobStageEvent(
                job_id=self.job.id,
                stage=stage,
                status="done",
                progress_pct=done_pct,
                message=message,
                detail_json=detail or {},
                duration_s=round(duration, 2),
            )
        )
        _set_job_meta(self.session, self.job, stage=stage, progress_pct=done_pct)
        self.session.commit()

    def fail(
        self,
        stage: str,
        message: str,
        error_code: str | None = None,
        detail: dict | None = None,
    ) -> None:
        duration = time.time() - self._started.pop(stage, time.time())
        self.session.add(
            JobStageEvent(
                job_id=self.job.id,
                stage=stage,
                status="failed",
                progress_pct=stage_progress_before(stage),
                message=message,
                error_code=error_code,
                detail_json=detail or {},
                duration_s=round(duration, 2),
            )
        )
        self.session.commit()

    def block(self, stage: str, error: MissingDependencyError, detail: dict | None = None) -> None:
        """Close a stage as ``blocked`` and put the job in blocked state."""
        duration = time.time() - self._started.pop(stage, time.time())
        merged = {"missing_dependency": error.to_dict(), **(detail or {})}
        self.session.add(
            JobStageEvent(
                job_id=self.job.id,
                stage=stage,
                status="blocked",
                progress_pct=stage_progress_before(stage),
                message=str(error),
                error_code=error.code,
                detail_json=merged,
                duration_s=round(duration, 2),
            )
        )
        self.job.status = JobStatus.BLOCKED.value
        self.job.error = str(error)
        _set_job_meta(
            self.session,
            self.job,
            stage=stage,
            blocked_by=error.to_dict(),
        )
        self.session.commit()


def stage_events_for(session: Session, job_id: str) -> list[JobStageEvent]:
    return (
        session.query(JobStageEvent)
        .filter_by(job_id=job_id)
        .order_by(JobStageEvent.created_at.asc())
        .all()
    )


def stage_event_out(event: JobStageEvent) -> dict:
    return {
        "id": event.id,
        "stage": event.stage,
        "status": event.status,
        "progress_pct": event.progress_pct,
        "message": event.message,
        "error_code": event.error_code,
        "detail": event.detail_json or {},
        "duration_s": event.duration_s,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def estimate_eta_s(session: Session, job: Job) -> float | None:
    """ETA (seconds) from the average total duration of finished runs.

    Returns None when no finished run exists yet — the API reports
    ``eta_s: null`` rather than inventing a number.
    """
    meta = _job_meta(job)
    progress = float(meta.get("progress_pct") or 0.0)
    if progress >= 100:
        return 0.0
    finished = (
        session.query(WorkerRun)
        .filter(WorkerRun.status.in_(("done", "failed", "blocked")))
        .filter(WorkerRun.duration_s.is_not(None))
        .order_by(WorkerRun.started_at.desc())
        .limit(20)
        .all()
    )
    if not finished:
        return None
    avg_total = sum(r.duration_s or 0.0 for r in finished) / len(finished)
    if avg_total <= 0:
        return None
    remaining = avg_total * (1.0 - progress / 100.0)
    return round(max(0.0, remaining), 1)


__all__ = [
    "STAGES",
    "STAGE_NAMES",
    "MIN_SCORE_THRESHOLD",
    "FFMPEG_DOWNLOAD_URL",
    "ffmpeg_missing_message",
    "ytdlp_missing_message",
    "stage_progress_before",
    "MissingDependencyError",
    "Dependency",
    "check_dependencies",
    "require_tool",
    "require_transcription_backend",
    "score_band",
    "StageTracker",
    "stage_events_for",
    "stage_event_out",
    "estimate_eta_s",
]
