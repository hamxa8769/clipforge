"""Batch queue endpoint (v0.8.0, Worker A).

Router: ``POST /batch``, ``GET /batch/{id}``,
``POST /batch/{id}/pause``, ``POST /batch/{id}/resume``.
Registered in ``clipforge_api/app.py`` by the coordinator — this
module never imports ``app.py`` (circular import: app.py registers
this router).

Rights guardrail: a batch queues sources the user already has (own
videos or Creative Commons / licensed / public-domain videos). The
queue never downloads or re-uploads another creator's video.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from clipforge.batch_queue import get_queue

router = APIRouter(prefix="/batch", tags=["batch"])


def _data_dir() -> Path:
    # Same resolution as clipforge_api.app._data_dir(): the env override
    # wins so tests can point at a temp dir; otherwise the configured
    # data dir. This module must not import app.py (circular import).
    from clipforge.config import settings  # local import: cheap, no cycle

    return Path(os.environ.get("CLIPFORGE_DATA_DIR") or settings.data_dir)


class BatchIn(BaseModel):
    """Create a batch from a list of sources."""

    sources: list[str]
    # Required for YouTube URLs: the batch only processes videos the
    # user owns or holds the rights to (same rule as POST /jobs).
    own_content_declaration: bool = False


def _is_youtube_url(source: str) -> bool:
    lowered = source.lower()
    return "youtube.com/watch" in lowered or "youtu.be/" in lowered


def _real_processor(item: dict, set_progress, is_paused) -> dict:
    """Run one batch item through the real pipeline (not simulated).

    Creates a Job for the source and runs ``worker.run_job`` on it with
    a fresh DB session. Local files and YouTube links are accepted;
    anything else fails with a plain-language message. The queue marks
    the item done/failed from the outcome; the returned dict lands in
    ``item.result`` (job id + clip count) so the UI can link to the job.
    """
    from clipforge.db import ClipCandidate, Job, JobStatus, get_session
    from clipforge.worker import run_job

    source = str(item.get("source") or "").strip()
    own_content = bool(item.get("own_content"))
    if _is_youtube_url(source):
        source_type, source_url, source_path = "youtube", source, None
    elif Path(source).exists():
        source_type, source_url, source_path = "local", None, source
    elif source.startswith(("http://", "https://")):
        raise ValueError(
            "Only local video files or YouTube links can be batched — "
            "paste a file path or a youtube.com / youtu.be link."
        )
    else:
        raise ValueError(f"Source not found: {source}")
    session = get_session()
    try:
        job = Job(
            source_type=source_type,
            source_url=source_url,
            source_path=source_path,
            own_content=own_content,
            status=JobStatus.PENDING.value,
            meta_json={"top_n": 3, "caption_style": "bold-pop"},
        )
        session.add(job)
        session.commit()
        set_progress(10)
        if is_paused():
            return {"job_id": job.id, "clips": 0, "status": job.status}
        summary = run_job(session, job.id, data_dir=_data_dir())
        session.refresh(job)
        clips = session.query(ClipCandidate).filter_by(job_id=job.id).count()
        return {
            "job_id": job.id,
            "clips": clips,
            "status": summary.get("status") or job.status,
            # The queue marks blocked/failed results "failed" (see
            # batch_queue._run_item): carry the plain-language reason.
            "error": job.error or "",
        }
    finally:
        session.close()


def _queue():
    """The shared batch queue, always with the real pipeline processor.

    Every handler goes through here (not bare ``get_queue(...)``):
    a read-only call must never be the one that creates the queue with
    the simulated default processor — see ``get_queue``.
    """
    return get_queue(_data_dir(), processor=_real_processor)


@router.post("", status_code=202)
def create_batch(payload: BatchIn):
    """Queue a batch of sources; returns the batch id at once.

    Processing happens on a background thread — poll
    ``GET /batch/{id}`` for progress. 422 (plain language) when
    no usable source is given, or when a YouTube link is queued
    without the own-content declaration.
    """
    if not payload.sources or not any(str(s).strip() for s in payload.sources):
        raise HTTPException(422, "Give at least one source to queue.")
    if any(_is_youtube_url(str(s)) for s in payload.sources) and (
        not payload.own_content_declaration
    ):
        raise HTTPException(
            422,
            "YouTube links need own_content_declaration=true: ClipForge "
            "only processes content you own or hold the rights to.",
        )
    try:
        batch_id = _queue().create_batch(
            payload.sources, own_content=payload.own_content_declaration
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    batch = _queue().get_batch(batch_id)
    return {"ok": True, "batch_id": batch_id, "batch": batch}


@router.get("/{batch_id}")
def get_batch(batch_id: str):
    """One batch: per-item states and progress 0-100."""
    batch = _queue().get_batch(batch_id)
    if batch is None:
        raise HTTPException(404, f"batch {batch_id} not found")
    return batch


@router.post("/{batch_id}/pause")
def pause_batch(batch_id: str):
    """Pause a batch: no new items start; in-flight items pause
    between stages."""
    batch = _queue().pause_batch(batch_id)
    if batch is None:
        raise HTTPException(404, f"batch {batch_id} not found")
    return {"ok": True, "batch": batch}


@router.post("/{batch_id}/resume")
def resume_batch(batch_id: str):
    """Resume a paused batch: paused items go back to queued."""
    batch = _queue().resume_batch(batch_id)
    if batch is None:
        raise HTTPException(404, f"batch {batch_id} not found")
    return {"ok": True, "batch": batch}


__all__ = ["router"]
