"""Background worker / queue for ClipForge v0.4.0.

The worker runs one job end to end — ingest → transcribe → score →
render → seo → review — and records everything it does so the
frontend can show a *live* workflow instead of a black box:

* every stage writes :class:`JobStageEvent` rows (started / done /
  failed / blocked) with progress %, elapsed time and detail;
* every run writes a :class:`WorkerRun` row (started, outcome,
  duration) — the queue view reads these;
* stage artifacts land in ``<data_dir>/jobs/<job_id>/``
  (``transcript.json``, ``candidates.json``) — the artbyjazi/autoclip
  pattern: a retry resumes from artifacts instead of redoing work
  (transcript reuse is via the sha256 transcript cache; scoring
  reuses ``candidates.json`` when the job already has candidates).

Queue shape (Postiz-design reference, own DB-backed implementation —
see docs/REUSE.md): jobs are the queue. ``pending`` / ``queued`` jobs
are waiting, a ``running`` :class:`WorkerRun` means a worker holds the
job, finished runs are history. This release runs the worker
in-process (``POST /jobs/{id}/run`` / ``POST /worker/run-next``) or
from the CLI (``python -m clipforge.worker run-next``); an
out-of-process daemon is a later step, and the tables already support
it (a second process would claim the oldest queued job the same way).

Blocked, never faked: a step whose dependency is missing (no FFmpeg,
no yt-dlp, no transcription backend) raises
:class:`MissingDependencyError`; the job goes to ``blocked`` with the
exact missing thing named, and the run is recorded as blocked. A
``transcript_text`` fallback run is flagged ``demo`` on the job and in
the stage detail — synthetic transcript, not real transcription.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from clipforge import automation
from clipforge.db import (
    CandidateSeo,
    ClipCandidate,
    Job,
    JobStageEvent,
    JobStatus,
    RetentionPoint,
    WorkerRun,
    get_session,
    init_db,
)
from clipforge.highlights import Candidate, find_candidates
from clipforge.ingest import IngestError, ingest_local, ingest_youtube
from clipforge.licensing import build_attribution_text, merge_records, record_from_obj
from clipforge.live import (
    LIVE_DEFAULT_DURATION_S,
    LiveCaptureCancelled,
    LiveCaptureError,
    capture_live_stream,
    check_can_clip,
    clear_live_cancel,
    live_cancel_event,
)
from clipforge.reframe import compute_crop_path
from clipforge.render import RenderError, render_clip
from clipforge.retention import adjust_candidate_scores, feedback_from_points
from clipforge.seo import generate_all as generate_all_seo
from clipforge.thumbnails import ThumbnailError, generate_thumbnails
from clipforge.transcribe import Transcript, transcribe
from clipforge.workflow import (
    FFMPEG_DOWNLOAD_URL,
    MIN_SCORE_THRESHOLD,
    MissingDependencyError,
    StageTracker,
    require_tool,
    require_transcription_backend,
)

RUNNABLE_STATUSES = {
    JobStatus.PENDING.value,
    JobStatus.QUEUED.value,
    JobStatus.FAILED.value,
    JobStatus.BLOCKED.value,
}
TERMINAL_RUN_STATUSES = ("done", "failed", "blocked", "cancelled")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _data_dir(data_dir: str | Path | None) -> Path:
    return Path(data_dir) if data_dir else Path(os.environ.get("CLIPFORGE_DATA_DIR", "data"))


def _job_dir(data_dir: Path, job_id: str) -> Path:
    path = data_dir / "jobs" / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_artifact(job_dir: Path, name: str, payload: dict) -> Path:
    path = job_dir / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _read_artifact(job_dir: Path, name: str) -> dict | None:
    path = job_dir / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return None


def _run_out(run: WorkerRun, job: Job) -> dict:
    return {
        "id": run.id,
        "job_id": run.job_id,
        "status": run.status,
        "outcome": run.outcome,
        "error": run.error,
        "duration_s": run.duration_s,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def queue_summary(session: Session, limit_runs: int = 10) -> dict:
    """What the worker is doing, what is queued, what ran last."""
    running = (
        session.query(WorkerRun)
        .filter_by(status="running")
        .order_by(WorkerRun.started_at.asc())
        .all()
    )
    queued = (
        session.query(Job)
        .filter(Job.status.in_([JobStatus.PENDING.value, JobStatus.QUEUED.value]))
        .order_by(Job.created_at.asc())
        .all()
    )
    recent = (
        session.query(WorkerRun)
        .filter(WorkerRun.status.in_(TERMINAL_RUN_STATUSES))
        .order_by(WorkerRun.started_at.desc())
        .limit(limit_runs)
        .all()
    )
    return {
        "running": [_run_out(r, session.get(Job, r.job_id)) for r in running],
        "queued": [
            {
                "id": j.id,
                "title": j.title,
                "source_type": j.source_type,
                "status": j.status,
                "created_at": j.created_at.isoformat() if j.created_at else None,
            }
            for j in queued
        ],
        "next": ({"id": queued[0].id, "title": queued[0].title} if queued else None),
        "recent_runs": [_run_out(r, session.get(Job, r.job_id)) for r in recent],
    }


def run_next_queued(session: Session, data_dir: str | Path | None = None) -> dict | None:
    """Run the oldest queued/pending job. Returns its summary, or None."""
    job = (
        session.query(Job)
        .filter(Job.status.in_([JobStatus.PENDING.value, JobStatus.QUEUED.value]))
        .order_by(Job.created_at.asc())
        .first()
    )
    if job is None:
        return None
    return run_job(session, job.id, data_dir=data_dir)


def run_job(session: Session, job_id: str, data_dir: str | Path | None = None) -> dict:
    """Run one job synchronously through every stage. Returns a summary.

    Raises :class:`ValueError` if the job does not exist or is not in a
    runnable status (already running / in review / ready / cancelled).
    Stage failures are recorded on the job, not raised — except a
    missing-dependency block, which is also recorded (status
    ``blocked``) and then returned in the summary.
    """
    job = session.get(Job, job_id)
    if job is None:
        raise ValueError(f"job {job_id} not found")
    if job.status not in RUNNABLE_STATUSES:
        raise ValueError(
            f"job {job_id} is '{job.status}' — only {sorted(RUNNABLE_STATUSES)} jobs can run"
        )

    data = _data_dir(data_dir)
    job_dir = _job_dir(data, job.id)
    meta = dict(job.meta_json or {})
    top_n = int(meta.get("top_n") or 3)
    caption_style = meta.get("caption_style") or "bold-pop"
    transcript_text = meta.get("transcript_text")
    started = time.time()

    run = WorkerRun(job_id=job.id, status="running", outcome="started")
    session.add(run)
    session.commit()
    tracker = StageTracker(session, job, data_dir=data)
    job.error = None
    meta.pop("blocked_by", None)
    job.meta_json = meta
    session.commit()

    summary: dict = {"job_id": job.id, "status": job.status, "demo": False}
    try:
        source, transcript, candidates = _run_stages(
            session,
            job,
            tracker,
            job_dir,
            data,
            top_n,
            caption_style,
            transcript_text,
            summary,
        )
    except LiveCaptureCancelled as exc:
        # The user cancelled a live capture mid-recording: the run is
        # cancelled (not failed) and the partial recording was deleted.
        _close_run(session, run, "cancelled", str(exc), started)
        summary.update(
            {
                "status": JobStatus.CANCELLED.value,
                "error": str(exc),
            }
        )
        return summary
    except LiveCaptureError as exc:
        # Already recorded as failed on the job with the plain message
        # by _run_stages — keep the message plain (no exception type
        # prefix) and close the run the same way.
        _close_run(session, run, "failed", str(exc), started)
        summary.update(
            {
                "status": JobStatus.FAILED.value,
                "error": str(exc),
            }
        )
        return summary
    except MissingDependencyError as exc:
        # Already recorded as blocked on the job + stage by _run_stages.
        _close_run(session, run, "blocked", str(exc), started)
        summary.update(
            {
                "status": JobStatus.BLOCKED.value,
                "blocked_by": exc.to_dict(),
                "error": str(exc),
            }
        )
        return summary
    except Exception as exc:  # unexpected failure — job failed, run failed
        job.status = JobStatus.FAILED.value
        job.error = f"{type(exc).__name__}: {exc}"
        session.commit()
        _close_run(session, run, "failed", job.error, started)
        summary.update({"status": JobStatus.FAILED.value, "error": job.error})
        return summary

    _close_run(
        session,
        run,
        "done",
        f"rendered {summary.get('rendered_count', 0)}/"
        f"{summary.get('candidate_count', 0)} clips; job in review",
        started,
    )
    summary["status"] = job.status
    return summary


def _close_run(session: Session, run: WorkerRun, status: str, outcome: str, started: float) -> None:
    run.status = status
    run.outcome = outcome
    run.duration_s = round(time.time() - started, 2)
    run.finished_at = _utcnow()
    if status in {"failed", "blocked"}:
        run.error = outcome
    session.commit()


def _run_stages(
    session: Session,
    job: Job,
    tracker: StageTracker,
    job_dir: Path,
    data: Path,
    top_n: int,
    caption_style: str,
    transcript_text: str | None,
    summary: dict,
) -> tuple[object, Transcript, list[Candidate]]:
    """The six stages. Raises MissingDependencyError on a blocked step."""
    # ---------------------------------------------------------- 1. ingest
    tracker.begin("ingest", f"ingest {job.source_type} source")
    try:
        if job.source_type == "youtube":
            require_tool(
                "yt-dlp",
                "yt-dlp downloads the YouTube source",
                "Install yt-dlp (pip install yt-dlp) or use a local file upload.",
            )
            source = ingest_youtube(
                job.source_url or "",
                own_content=bool(job.own_content),
                out_dir=data / "uploads",
            )
        elif job.source_type == "live":
            # Live capture (v0.7.0): record a bounded segment of the
            # live stream, then the normal stages run on the recording.
            # The licence was verified by the capture endpoint, but the
            # worker re-checks the stored meta — a standard-licence
            # third-party stream is never captured, whatever the job
            # row claims.
            require_tool(
                "yt-dlp",
                "yt-dlp records the live stream",
                "Install yt-dlp (pip install yt-dlp) or use a local file upload.",
            )
            live_meta = dict(job.meta_json or {})
            try:
                check_can_clip(
                    str(live_meta.get("live_licence") or "unknown"),
                    bool(live_meta.get("live_is_own")),
                )
            except LiveCaptureError as exc:
                tracker.fail("ingest", str(exc), error_code="live_not_clippable")
                job.status = JobStatus.FAILED.value
                job.error = str(exc)
                session.commit()
                raise
            duration = int(live_meta.get("live_duration_s") or LIVE_DEFAULT_DURATION_S)
            last_pct = {"value": -1.0}

            def _capture_progress(frac: float) -> None:
                # Ingest is 10% of the job: report capture progress
                # inside it, throttled to whole-percent changes so a
                # 300s capture does not commit hundreds of times.
                pct = round(frac * 10.0, 1)
                if pct - last_pct["value"] >= 1.0:
                    last_pct["value"] = pct
                    _set_job_meta(session, job, progress_pct=pct)

            try:
                source = capture_live_stream(
                    job.source_url or "",
                    out_dir=data / "uploads",
                    duration_s=duration,
                    cancel_event=live_cancel_event(job.id),
                    on_progress=_capture_progress,
                )
            except LiveCaptureCancelled as exc:
                tracker.fail("ingest", str(exc), error_code="live_cancelled")
                job.status = JobStatus.CANCELLED.value
                job.error = str(exc)
                session.commit()
                raise
            except LiveCaptureError as exc:
                tracker.fail("ingest", str(exc), error_code="live_capture_failed")
                job.status = JobStatus.FAILED.value
                job.error = str(exc)
                session.commit()
                raise
            finally:
                clear_live_cancel(job.id)
        else:
            require_tool(
                "ffprobe",
                "ffprobe validates the local source file",
                f"Install FFmpeg from {FFMPEG_DOWNLOAD_URL} "
                "(ffprobe ships with it) and make sure it is on PATH.",
            )
            source = ingest_local(str(job.source_path or job.source_url or ""))
    except MissingDependencyError as exc:
        tracker.block("ingest", exc)
        raise
    except IngestError as exc:
        tracker.fail("ingest", str(exc), error_code="ingest_failed")
        job.status = JobStatus.FAILED.value
        job.error = str(exc)
        session.commit()
        raise
    tracker.finish(
        "ingest",
        f"ingested {source.format_name} ({source.duration_s:.1f}s, {source.width}x{source.height})",
        detail={"source": source.to_dict()},
    )
    job.source_path = str(source.path)
    session.commit()

    # ------------------------------------------------------ 2. transcribe
    tracker.begin("transcribe", "transcribe to word-level transcript")
    demo = False
    try:
        if transcript_text:
            # Deterministic fallback: caller-supplied text spread over the
            # duration. Flagged demo — never presented as real Whisper.
            # NOTE: use_cache=False on purpose. The shared transcript
            # cache is keyed by video sha256 only, so a cached transcript
            # would silently win over a *different* transcript_text for
            # the same video — and a cached fallback transcript would
            # later poison a real Whisper run of the same video.
            transcript = transcribe(
                source.path,
                source_sha256=source.sha256,
                duration_s=source.duration_s,
                transcript_text=transcript_text,
                force_engine="fallback-text",
                cache_dir=data / "transcripts",
                use_cache=False,
            )
            demo = True
        else:
            require_transcription_backend()
            transcript = transcribe(
                source.path,
                source_sha256=source.sha256,
                duration_s=source.duration_s,
                cache_dir=data / "transcripts",
            )
            demo = transcript.engine == "fallback-text"
    except MissingDependencyError as exc:
        tracker.block("transcribe", exc, detail={"engine": "none"})
        raise
    except RuntimeError as exc:
        # transcribe() raises RuntimeError when no backend is available.
        if "FFmpeg is missing" in str(exc):
            # Audio extraction needs the ffmpeg binary (not just a
            # transcription backend): a blocked job, not a failed one,
            # with the install link in the required action.
            dep = MissingDependencyError(
                code="ffmpeg_missing",
                dependency="ffmpeg",
                kind="tool",
                detail=str(exc),
                required_action=(
                    f"Install FFmpeg from {FFMPEG_DOWNLOAD_URL} "
                    "and make sure it is on PATH, then retry the job."
                ),
            )
            tracker.block("transcribe", dep)
            raise dep
        if "no transcription backend" in str(exc):
            dep = MissingDependencyError(
                code="transcription_backend_missing",
                dependency="faster-whisper or OPENAI_API_KEY",
                kind="package",
                detail=str(exc),
                required_action="Install faster-whisper or set OPENAI_API_KEY, "
                "or supply transcript_text for the demo fallback.",
            )
            tracker.block("transcribe", dep)
            raise dep
        tracker.fail("transcribe", str(exc), error_code="transcribe_failed")
        job.status = JobStatus.FAILED.value
        job.error = str(exc)
        session.commit()
        raise
    _write_artifact(
        job_dir,
        "transcript.json",
        {"engine": transcript.engine, "demo": demo, **transcript.to_dict()},
    )
    meta = dict(job.meta_json or {})
    meta["demo"] = demo
    job.meta_json = meta
    session.commit()
    summary["demo"] = demo
    tracker.finish(
        "transcribe",
        f"{len(transcript.words)} words via {transcript.engine}"
        + (" (demo transcript — supplied text, not Whisper)" if demo else ""),
        detail={"engine": transcript.engine, "demo": demo, "word_count": len(transcript.words)},
    )

    # ---------------------------------------------------------- 3. score
    tracker.begin("score", "score viral moments")
    stored = session.query(ClipCandidate).filter_by(job_id=job.id).all()
    artifact = _read_artifact(job_dir, "candidates.json")
    if stored and artifact:
        # Resume: this job already scored — reuse the artifact + DB rows.
        candidates = [
            Candidate(
                start_word_index=0,
                end_word_index=0,
                start_s=c.start_s,
                end_s=c.end_s,
                score=c.score,
                hook=int(c.hook_rating or 5),
                hook_line=c.hook_line,
                reason=c.reason,
            )
            for c in stored
        ]
        tracker.finish(
            "score",
            f"resumed {len(candidates)} scored candidates from stage artifact",
            detail={"resumed": True, "candidate_count": len(candidates)},
        )
        summary["candidate_count"] = len(candidates)
        summary["alternate_count"] = len((artifact or {}).get("alternates") or [])
    else:
        found = find_candidates(transcript, top_n=max(top_n, 10))
        # Retention feedback: early drop-off penalises hooks, retaining
        # formats boost signals, poor completion shortens the target.
        feedback = feedback_from_points(
            session.query(RetentionPoint)
            .order_by(RetentionPoint.created_at.desc())
            .limit(200)
            .all()
        )
        adjusted = adjust_candidate_scores(
            [
                {
                    "score": c.score,
                    "hook": c.hook,
                    "signals": c.signals,
                    "duration_s": c.duration_s,
                    "start_s": c.start_s,
                    "end_s": c.end_s,
                    "hook_line": c.hook_line,
                    "reason": c.reason,
                }
                for c in found
            ],
            feedback,
        )
        qualifying = [a for a in adjusted if a["adjusted_score"] >= MIN_SCORE_THRESHOLD]
        alternates = [a for a in adjusted if a["adjusted_score"] < MIN_SCORE_THRESHOLD]
        # Keep at most top_n qualifying candidates for review.
        kept = qualifying[:top_n]
        # Re-attach the original Candidate objects in adjusted order.
        by_span = {(c.start_s, c.end_s): c for c in found}
        candidates = []
        session.query(ClipCandidate).filter_by(job_id=job.id).delete()
        for rank, adj in enumerate(kept, start=1):
            orig = by_span[(adj["start_s"], adj["end_s"])]
            reason = orig.reason
            if adj["adjustment"] != 0:
                reason = f"{reason} [{adj['adjustment_reason']}]"
            row = ClipCandidate(
                job_id=job.id,
                n=rank,
                start_s=orig.start_s,
                end_s=orig.end_s,
                score=adj["adjusted_score"],
                hook_rating=float(orig.hook),
                hook_line=orig.hook_line,
                reason=reason,
                transcript_excerpt=orig.hook_line,
                status="pending",
            )
            session.add(row)
            candidates.append(
                Candidate(
                    start_word_index=orig.start_word_index,
                    end_word_index=orig.end_word_index,
                    start_s=orig.start_s,
                    end_s=orig.end_s,
                    score=adj["adjusted_score"],
                    hook=orig.hook,
                    hook_line=orig.hook_line,
                    reason=reason,
                    signals=orig.signals,
                    source=orig.source,
                )
            )
        session.commit()
        _write_artifact(
            job_dir,
            "candidates.json",
            {
                "candidates": [c.to_dict() for c in candidates],
                "alternates": [
                    {
                        "score": a["adjusted_score"],
                        "hook_line": a["hook_line"],
                        "start_s": a["start_s"],
                        "end_s": a["end_s"],
                        "reason": a["reason"],
                    }
                    for a in alternates
                ],
                "feedback": feedback.to_dict(),
            },
        )
        summary["candidate_count"] = len(candidates)
        summary["alternate_count"] = len(alternates)
        tracker.finish(
            "score",
            f"{len(candidates)} qualifying candidates, "
            f"{len(alternates)} alternates below {MIN_SCORE_THRESHOLD:.0f}",
            detail={
                "candidate_count": len(candidates),
                "alternates": [
                    {
                        "score": a["adjusted_score"],
                        "hook_line": a["hook_line"],
                        "start_s": a["start_s"],
                        "end_s": a["end_s"],
                    }
                    for a in alternates
                ],
                "feedback": feedback.to_dict(),
            },
        )
        # High-score auto-approve (rule OFF by default): a clip clearing
        # 95+ score and 9+ hook is approved without human review. It
        # still goes through the normal approval hooks (SEO + queue) and
        # post-level approval before publishing still applies.
        auto_approved = 0
        for row in session.query(ClipCandidate).filter_by(job_id=job.id).all():
            if automation.apply_high_score_rule(session, row):
                auto_approved += 1
                automation.on_candidate_approved(session, row)
        summary["auto_approved"] = auto_approved

    # --------------------------------------------------------- 4. render
    tracker.begin("render", f"render {len(candidates)} clips to 9:16")
    try:
        require_tool(
            "ffmpeg",
            "FFmpeg renders the clips",
            f"Install FFmpeg from {FFMPEG_DOWNLOAD_URL} "
            "and make sure it is on PATH, then retry the job.",
        )
    except MissingDependencyError as exc:
        tracker.block("render", exc)
        raise
    out_dir = data / "clips" / job.id
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered = 0
    render_detail: list[dict] = []
    stored_rows = (
        session.query(ClipCandidate).filter_by(job_id=job.id).order_by(ClipCandidate.n.asc()).all()
    )
    for row in stored_rows:
        cand = Candidate(
            start_word_index=0,
            end_word_index=0,
            start_s=row.start_s,
            end_s=row.end_s,
            score=row.score,
            hook=int(row.hook_rating or 5),
            hook_line=row.hook_line,
            reason=row.reason,
        )
        out_path = out_dir / f"clip_{row.n:02d}.mp4"
        try:
            plan = compute_crop_path(
                str(source.path),
                source.duration_s,
                source.width,
                source.height,
                start_s=cand.start_s,
                end_s=cand.end_s,
            )
            result = render_clip(
                source.path,
                cand,
                out_path,
                style=caption_style,
                words=transcript.words,
                plan=plan,
            )
            row.file_path = str(result.path)
            session.commit()
            rendered += 1
            render_detail.append({"n": row.n, "status": "rendered", "file": str(result.path)})
        except (RenderError, Exception) as exc:
            render_detail.append({"n": row.n, "status": "failed", "error": str(exc)})
    summary["rendered_count"] = rendered
    if stored_rows and rendered == 0:
        tracker.fail(
            "render",
            "all clips failed to render",
            error_code="render_failed",
            detail={"clips": render_detail},
        )
        job.status = JobStatus.FAILED.value
        job.error = "all clips failed to render: " + "; ".join(
            d.get("error", "?") for d in render_detail
        )
        session.commit()
        raise RuntimeError(job.error)
    tracker.finish(
        "render",
        f"rendered {rendered}/{len(stored_rows)} clips",
        detail={"clips": render_detail},
    )

    # ------------------------------------------- 4b. licence + thumbnails
    # v0.7.0: every rendered clip inherits the job's licence record, and
    # gets two deterministic thumbnails (25% / 75% frames, title overlaid)
    # next to the clip file. A thumbnail failure never fails the job —
    # the clip is still reviewable, the detail says why there is no
    # thumbnail, and POST /clips/<id>/thumbnail can regenerate it.
    job_licence = record_from_obj(job)
    thumb_detail: list[dict] = []
    for row in stored_rows:
        record = merge_records(record_from_obj(row), job_licence)
        row.source_license = record["source_license"]
        row.source_url = record["source_url"]
        row.source_title = record["source_title"]
        row.source_channel = record["source_channel"]
        row.attribution_text = record["attribution_text"]
        if row.file_path:
            try:
                variants = generate_thumbnails(
                    row.file_path,
                    out_dir,
                    title=job.title or row.hook_line or "",
                    start_s=row.start_s,
                    end_s=row.end_s,
                )
                row.thumbnail_json = variants
                row.thumbnail_path = variants[0]["path"] if variants else None
                thumb_detail.append({"n": row.n, "status": "ok", "variants": len(variants)})
            except ThumbnailError as exc:
                thumb_detail.append({"n": row.n, "status": "failed", "error": str(exc)})
        else:
            thumb_detail.append({"n": row.n, "status": "skipped", "error": "clip not rendered"})
    session.commit()
    summary["thumbnails"] = thumb_detail

    # ------------------------------------------------------------ 5. seo
    tracker.begin("seo", "generate per-platform SEO variants")
    seo_count = 0
    keyword = (job.title or "").split(":")[0].strip() or "clip"
    for row in stored_rows:
        attribution = build_attribution_text(
            row.source_title or job.title,
            row.source_channel,
            row.source_url or job.source_url,
            row.source_license,
        )
        variants = generate_all_seo(
            keyword,
            row.hook_line or "",
            row.hook_line or "",
            row.duration_s or 30.0,
            attribution_text=attribution,
        )
        for platform, variant in variants.items():
            existing = (
                session.query(CandidateSeo)
                .filter_by(clip_candidate_id=row.id, platform=platform)
                .first()
            )
            if existing:
                existing.seo_json = variant
            else:
                session.add(
                    CandidateSeo(
                        clip_candidate_id=row.id,
                        platform=platform,
                        seo_json=variant,
                        generated_by="worker",
                    )
                )
            seo_count += 1
    session.commit()
    tracker.finish("seo", f"stored {seo_count} SEO variants", detail={"variants": seo_count})

    # --------------------------------------------------------- 6. review
    # If the high-score rule auto-approved a clip, the job is ready to
    # schedule (same transition as a human approval); otherwise it waits
    # in review for a human to pick.
    auto_approved = summary.get("auto_approved", 0)
    if auto_approved:
        job.status = JobStatus.READY.value
        session.add(
            JobStageEvent(
                job_id=job.id,
                stage="review",
                status="done",
                progress_pct=100.0,
                message=f"{auto_approved} clip(s) auto-approved by the "
                "high-score rule — job ready to schedule",
            )
        )
        session.commit()
        _set_job_meta(session, job, stage="review", progress_pct=100.0)
    else:
        job.status = JobStatus.REVIEW.value
        session.commit()
        _set_job_meta(session, job, stage="review", progress_pct=95.0)
    return source, transcript, candidates


def _set_job_meta(session: Session, job: Job, **updates: object) -> None:
    meta = dict(job.meta_json or {})
    meta.update(updates)
    job.meta_json = meta
    session.commit()


# ------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClipForge worker — run queued jobs")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--data-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="run one job by id")
    run_p.add_argument("job_id")
    sub.add_parser("run-next", help="run the oldest queued job")
    sub.add_parser("queue", help="show running / queued / recent runs")
    args = parser.parse_args(argv)
    init_db(args.database_url)
    session = get_session(args.database_url)
    try:
        if args.command == "run":
            print(run_job(session, args.job_id, data_dir=args.data_dir))
        elif args.command == "run-next":
            result = run_next_queued(session, data_dir=args.data_dir)
            print(result if result is not None else "queue empty — nothing to run")
        elif args.command == "queue":
            print(json.dumps(queue_summary(session), indent=2, default=str))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "run_job",
    "run_next_queued",
    "queue_summary",
    "main",
    "RUNNABLE_STATUSES",
]
