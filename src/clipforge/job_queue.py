"""The real job queue for ClipForge v0.9.0.

Before this module, ``POST /jobs/{id}/run`` ran the pipeline inline
in the request thread: two jobs started at once ran in parallel on
the same ffmpeg / yt-dlp, a restart lost running jobs silently, and
there was no pause / resume / cancel. This module is the fix:

* **One active job at a time.** A single dispatcher thread owns the
  pipeline. Every run started through the API — user-started,
  batch item, ``run-next`` — goes through :func:`get_job_queue`. A
  second job started while one runs is enqueued with an honest
  ``"Queued — position N"`` state, never started in parallel. (The
  ``python -m clipforge.worker`` CLI and the agent run the pipeline
  directly — they are the manual / internal paths, not the queue.)
* **FIFO with priority for user-started runs.** Order is
  ``queue_priority`` desc, ``created_at`` asc (see
  :func:`clipforge.worker.queue_order_key`); the priority lives in
  ``meta_json`` so the order survives a restart.
* **Durable state machine.** ``queued → running → paused → running →
  done/failed/cancelled`` is DB state (``Job.status``), not memory.
  The dispatcher claims a job by setting ``running`` before the
  pipeline starts, so a second dispatcher (or a second request)
  cannot claim the same job.
* **Restart-safe.** On first use the queue reconciles: any job left
  in an active status (``running`` / ``transcribing`` / ``scoring`` /
  ``rendering`` / ``paused``) by a crash or restart goes back to
  ``queued`` and its orphan ``running`` WorkerRun is closed as
  ``interrupted`` — the job resumes from its stage artifacts on the
  next run (transcript / candidates reuse) instead of silently
  restarting or running twice.
* **Pause / resume / cancel that work.** See
  :mod:`clipforge.job_control`: pause waits between stages and
  between clips, cancel kills the live ffmpeg / yt-dlp process.

The queue is in-process (one dispatcher thread in the API server).
A second server process would need a DB claim — out of scope for
this desktop release; the sidecar runs exactly one server.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from clipforge.db import Job, JobStatus, WorkerRun, get_session
from clipforge.job_control import drop_control, get_control
from clipforge.notifications import notify
from clipforge.worker import ACTIVE_JOB_STATUSES, queue_order_key, run_job

# Statuses a job may be enqueued from. A job in review / ready /
# cancelled has finished (or was stopped) — starting it again is the
# retry endpoint's job, not the queue's.
ENQUEUEABLE_STATUSES = {
    JobStatus.PENDING.value,
    JobStatus.QUEUED.value,
}

# Statuses a job may be cancelled from (anything not terminal).
CANCELLABLE_STATUSES = (
    ENQUEUEABLE_STATUSES
    | ACTIVE_JOB_STATUSES
    | {
        JobStatus.PAUSED.value,
        JobStatus.FAILED.value,
        JobStatus.BLOCKED.value,
    }
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _data_dir(data_dir: str | Path | None) -> Path:
    return Path(data_dir) if data_dir else Path(os.environ.get("CLIPFORGE_DATA_DIR", "data"))


class JobQueue:
    """One dispatcher thread + the durable queue state in the DB."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        runner=None,
    ) -> None:
        self.data_dir = _data_dir(data_dir)
        # An explicit dir (tests pass a tmp dir) is pinned. A queue
        # created without one follows CLIPFORGE_DATA_DIR as it is at
        # RUN time, not creation time: the API singleton is created
        # once per process, but tests re-point the env var per module
        # — pinning the first module's dir would write the next
        # module's job artifacts into the wrong directory.
        self._explicit_data_dir = data_dir is not None
        # ``runner(session, job_id, data_dir, control)`` — defaults to
        # the real pipeline; tests inject a fake runner.
        self._runner = runner or run_job
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._events: dict[str, threading.Event] = {}
        self._active_job_id: str | None = None
        self._stop = threading.Event()
        # Reconcile NOW, synchronously, before the dispatcher starts:
        # jobs left mid-flight by a previous process go back to the
        # queue. It must never run later — a job the *user* paused in
        # this process is a legitimate state, not a crash leftover,
        # and a lazy reconcile would silently un-pause it.
        session = get_session()
        try:
            self.reconcile(session)
        finally:
            session.close()
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="clipforge-job-dispatcher", daemon=True
        )
        self._dispatcher.start()

    # ------------------------------------------------------------ lifecycle
    def reconcile(self, session: Session) -> list[str]:
        """Re-queue jobs left mid-flight by a crash / restart.

        Returns the ids that were re-queued. A job found in an active
        status was running when the server died: it goes back to
        ``queued`` (its stage artifacts survive, so the next run
        resumes instead of redoing work) and its orphan ``running``
        WorkerRun is closed as ``interrupted``. A notification is
        written per job so the restart is visible, not silent.
        """
        requeued: list[str] = []
        interrupted = (
            session.query(Job).filter(Job.status.in_(sorted(ACTIVE_JOB_STATUSES))).all()
        )
        paused = session.query(Job).filter_by(status=JobStatus.PAUSED.value).all()
        for job in interrupted + paused:
            job.status = JobStatus.QUEUED.value
            meta = dict(job.meta_json or {})
            meta["interrupted"] = True
            meta["queue_priority"] = int(meta.get("queue_priority") or 0)
            job.meta_json = meta
            requeued.append(job.id)
        for run in session.query(WorkerRun).filter_by(status="running").all():
            run.status = "interrupted"
            run.outcome = (
                "Server restarted while this job was running — the job "
                "was put back in the queue, no work was lost."
            )
            run.finished_at = _utcnow()
        session.commit()
        for job_id in requeued:
            job = session.get(Job, job_id)
            if job is not None:
                try:
                    notify(
                        session,
                        "warning",
                        "Job interrupted by restart",
                        f"“{job.title or job_id}” was running when ClipForge "
                        "restarted. It is back in the queue and will resume "
                        "from where it stopped.",
                        action={"label": "Open job", "url": f"/jobs/{job_id}"},
                        job_id=job_id,
                    )
                except Exception:
                    pass
        return requeued

    def _reconcile_once(self, session: Session) -> None:
        # Reconciliation runs synchronously in __init__ (see above);
        # kept as a no-op guard so a second call can never un-pause a
        # job the user paused in this process.
        return

    def shutdown(self) -> None:
        """Stop the dispatcher (tests / clean shutdown). Idempotent."""
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        # Join: without this the thread can still be alive (up to its
        # 0.2 s poll) and claim one more job after "shutdown" — in
        # tests, where several queues share one DB, that steals the
        # next test's jobs. Never join from the dispatcher itself.
        if threading.current_thread() is not self._dispatcher:
            self._dispatcher.join(timeout=10)

    # ------------------------------------------------------------ queue state
    def queue_position(self, session: Session, job_id: str) -> int | None:
        """1-based position of a queued job, or None if not queued."""
        queued = sorted(
            session.query(Job)
            .filter(Job.status.in_([JobStatus.PENDING.value, JobStatus.QUEUED.value]))
            .all(),
            key=queue_order_key,
        )
        for i, job in enumerate(queued):
            if job.id == job_id:
                return i + 1
        return None

    def queue_state(self, session: Session) -> dict:
        """Active job + queued jobs with positions + paused + recent runs.

        Extends :func:`clipforge.worker.queue_summary` with the
        dispatcher's live view (``active_job_id``) so the UI can tell
        "running now" apart from "waiting".
        """
        from clipforge.worker import queue_summary  # noqa: PLC0415 (same package)

        state = queue_summary(session)
        state["active_job_id"] = self._active_job_id
        return state

    # ------------------------------------------------------------ enqueue
    def enqueue(
        self, session: Session, job_id: str, priority: int = 0
    ) -> dict:
        """Put a job in the queue. Returns ``{job_id, status, queue_position}``.

        Idempotent: a job already queued keeps its place (priority is
        only raised, never lowered). A running / in-review / finished
        job raises :class:`ValueError` — it cannot be queued again.
        """
        job = session.get(Job, job_id)
        if job is None:
            raise ValueError(f"job {job_id} not found")
        if job.status in ACTIVE_JOB_STATUSES or job.status == JobStatus.RUNNING.value:
            raise ValueError(f"job {job_id} is already running")
        if job.status not in ENQUEUEABLE_STATUSES:
            raise ValueError(
                f"job {job_id} is '{job.status}' — only pending / queued jobs can be queued"
            )
        meta = dict(job.meta_json or {})
        meta["queue_priority"] = max(int(meta.get("queue_priority") or 0), int(priority))
        meta["enqueued_at"] = _utcnow().isoformat()
        meta.pop("interrupted", None)
        # Conditional write: only flip the row if it is still
        # pending / queued. A dispatcher that claimed the job after
        # our read wins — the UPDATE touches zero rows and the job
        # must NOT be written back to queued (that would resurrect a
        # running job and run it twice).
        updated = (
            session.query(Job)
            .filter(
                Job.id == job_id,
                Job.status.in_(
                    [JobStatus.PENDING.value, JobStatus.QUEUED.value]
                ),
            )
            .update(
                {"status": JobStatus.QUEUED.value, "meta_json": meta},
                synchronize_session=False,
            )
        )
        session.commit()
        # The bulk update bypassed the identity map — expire the
        # loaded object so the re-read / queue_position below see
        # the committed status and priority, not the stale ones.
        session.expire(job)
        if not updated:
            # Claimed in the meantime — report its real state.
            fresh = session.get(Job, job_id)
            if fresh is not None and (
                fresh.status in ACTIVE_JOB_STATUSES
                or fresh.status == JobStatus.RUNNING.value
            ):
                raise ValueError(f"job {job_id} is already running")
            # A concurrent enqueue already applied — idempotent.
            return {
                "job_id": job_id,
                "status": JobStatus.QUEUED.value,
                "queue_position": self.queue_position(session, job_id),
            }
        with self._condition:
            # A new run starts unfinished: clear any event left set
            # by a previous run, or wait_for() below would return at
            # once on the stale signal and report the job as done
            # while it is only queued (retry after a finished run).
            self._events.setdefault(job_id, threading.Event()).clear()
            self._condition.notify_all()
        return {
            "job_id": job.id,
            "status": JobStatus.QUEUED.value,
            "queue_position": self.queue_position(session, job_id),
        }

    def wait_for(
        self, job_id: str, timeout: float | None = None, pause_check=None
    ) -> bool:
        """Block until the job reaches a terminal state (or timeout).

        ``pause_check`` (optional, used by the batch processor) is
        called every 0.25 s: while it returns True the job is paused,
        when it returns False again the job resumes. Returns True when
        the job finished, False on timeout.
        """
        with self._lock:
            event = self._events.setdefault(job_id, threading.Event())
        paused = False
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if event.wait(timeout=min(0.25, remaining) if remaining is not None else 0.25):
                return True
            if pause_check is not None:
                session = get_session()
                try:
                    if pause_check() and not paused:
                        self.pause(session, job_id)
                        paused = True
                    elif not pause_check() and paused:
                        self.resume(session, job_id)
                        paused = False
                except ValueError:
                    pass  # job finished / left the queue meanwhile
                finally:
                    session.close()
            if deadline is not None and time.monotonic() >= deadline:
                return False

    def enqueue_and_wait(
        self,
        session: Session,
        job_id: str,
        priority: int = 0,
        timeout: float | None = None,
        pause_check=None,
    ) -> dict:
        """Enqueue a job and wait until it finishes. Returns its summary.

        Used by ``POST /jobs/{id}/run`` (priority 1, the user asked for
        it now) so the endpoint keeps its synchronous contract while
        the dispatcher guarantees only one job runs at a time.
        """
        self.enqueue(session, job_id, priority=priority)
        self.wait_for(job_id, timeout=timeout, pause_check=pause_check)
        fresh = get_session()
        try:
            job = fresh.get(Job, job_id)
            if job is None:
                raise ValueError(f"job {job_id} not found")
            return {"job_id": job.id, "status": job.status, "error": job.error}
        finally:
            fresh.close()

    def run_next(self, session: Session) -> dict | None:
        """Enqueue the oldest pending/queued job and wait for it.

        Returns its summary, or None when the queue is empty.
        """
        candidates = sorted(
            session.query(Job)
            .filter(Job.status.in_([JobStatus.PENDING.value, JobStatus.QUEUED.value]))
            .all(),
            key=queue_order_key,
        )
        if not candidates:
            return None
        return self.enqueue_and_wait(session, candidates[0].id, priority=1)

    # ------------------------------------------------------------ control
    def pause(self, session: Session, job_id: str) -> dict:
        """Pause a queued or running job.

        Running: the flag is set on the job's control object; the
        worker pauses between stages / clips and the job status
        becomes ``paused``. Queued: the job becomes ``paused`` and the
        dispatcher skips it until resumed. Returns the job state.
        """
        job = session.get(Job, job_id)
        if job is None:
            raise ValueError(f"job {job_id} not found")
        if job.status == JobStatus.PAUSED.value:
            return {"job_id": job.id, "status": job.status}
        if job.status in ACTIVE_JOB_STATUSES or job.status == JobStatus.RUNNING.value:
            get_control(job_id).request_pause()
            # The worker flips the status to "paused" at its next
            # checkpoint; set it now too so the UI answers at once.
            job.status = JobStatus.PAUSED.value
            session.commit()
            return {"job_id": job.id, "status": job.status}
        if job.status in ENQUEUEABLE_STATUSES:
            # Conditional write: a dispatcher that claimed the job
            # after our read wins — the UPDATE touches zero rows and
            # we fall through to the running branch (pause flag) so
            # the claim is not overwritten back to queued.
            updated = (
                session.query(Job)
                .filter(Job.id == job_id, Job.status == job.status)
                .update(
                    {"status": JobStatus.PAUSED.value},
                    synchronize_session=False,
                )
            )
            session.commit()
            if not updated:
                get_control(job_id).request_pause()
                session.query(Job).filter_by(id=job_id).update(
                    {"status": JobStatus.PAUSED.value},
                    synchronize_session=False,
                )
                session.commit()
            return {"job_id": job.id, "status": JobStatus.PAUSED.value}
        raise ValueError(f"job {job_id} is '{job.status}' — cannot pause from here")

    def resume(self, session: Session, job_id: str) -> dict:
        """Resume a paused job: back to ``queued`` (or running).

        A job paused while running keeps running once its flag clears
        — the worker is still inside its pause wait, so resume sets
        the status back to ``running`` and wakes it. A job paused
        while queued goes back to ``queued`` and the dispatcher picks
        it up in order.
        """
        job = session.get(Job, job_id)
        if job is None:
            raise ValueError(f"job {job_id} not found")
        if job.status != JobStatus.PAUSED.value:
            raise ValueError(f"job {job_id} is '{job.status}' — only paused jobs can resume")
        control = get_control(job_id)
        was_running = control.is_paused and self._active_job_id == job_id
        control.request_resume()
        job.status = (
            JobStatus.RUNNING.value if was_running else JobStatus.QUEUED.value
        )
        session.commit()
        with self._condition:
            self._condition.notify_all()
        return {"job_id": job.id, "status": job.status}

    def cancel(self, session: Session, job_id: str) -> dict:
        """Cancel a job: queued → cancelled at once; running → the
        worker is told to stop (ffmpeg / yt-dlp killed) and the job
        lands in ``cancelled`` as soon as the current step unwinds.

        Returns ``{job_id, status}`` where a running job reports
        ``cancelling`` until the worker confirms the cancel.
        """
        job = session.get(Job, job_id)
        if job is None:
            raise ValueError(f"job {job_id} not found")
        if job.status == JobStatus.CANCELLED.value:
            raise ValueError(f"job {job_id} is already cancelled")
        if job.status not in CANCELLABLE_STATUSES:
            raise ValueError(f"job {job_id} is '{job.status}' — cannot cancel from here")
        if job.status in ACTIVE_JOB_STATUSES or job.status == JobStatus.RUNNING.value:
            get_control(job_id).request_cancel()
            # Live captures watch their own event, not the control.
            from clipforge.live import request_live_cancel  # noqa: PLC0415

            request_live_cancel(job_id)
            return {"job_id": job.id, "status": "cancelling"}
        if job.status == JobStatus.PAUSED.value and self._active_job_id == job_id:
            # Paused mid-run: the worker is asleep in its pause wait;
            # cancelling wakes it and it exits as cancelled.
            get_control(job_id).request_cancel()
            return {"job_id": job.id, "status": "cancelling"}
        # Direct cancel — but only if the row still has the status we
        # read. A dispatcher that claimed the job after our read wins
        # the race: the UPDATE touches zero rows, and writing
        # "cancelled" over the claim would leave a running job whose
        # worker never saw the cancel flag (it would finish as
        # done/failed instead). Fall through to the flag instead.
        updated = (
            session.query(Job)
            .filter(Job.id == job_id, Job.status == job.status)
            .update(
                {
                    "status": JobStatus.CANCELLED.value,
                    "error": "Cancelled by the user.",
                },
                synchronize_session=False,
            )
        )
        session.query(WorkerRun).filter_by(job_id=job.id, status="running").update(
            {"status": "cancelled", "outcome": "cancelled by user"}
        )
        session.commit()
        if not updated:
            get_control(job_id).request_cancel()
            # Live captures watch their own event, not the control.
            from clipforge.live import request_live_cancel  # noqa: PLC0415

            request_live_cancel(job_id)
            return {"job_id": job.id, "status": "cancelling"}
        with self._lock:
            event = self._events.get(job_id)
        if event is not None:
            event.set()
        return {"job_id": job.id, "status": JobStatus.CANCELLED.value}

    # ------------------------------------------------------------ dispatcher
    def _next_queued(self, session: Session) -> Job | None:
        queued = sorted(
            session.query(Job).filter_by(status=JobStatus.QUEUED.value).all(),
            key=queue_order_key,
        )
        return queued[0] if queued else None

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._dispatch_once()
            except Exception:
                # The dispatcher must never die on a bad job: log to
                # stderr and keep the queue moving.
                import traceback

                traceback.print_exc()
            with self._condition:
                self._condition.wait(timeout=0.2)

    def _dispatch_once(self) -> None:
        session = get_session()
        try:
            if self._active_job_id is not None:
                return  # one active job at a time — never two
            job = self._next_queued(session)
            if job is None:
                return
            job_id = job.id
            # Atomic claim: flip queued -> running only if the row is
            # still queued. A cancel / pause that landed after the
            # SELECT above wins the race — the UPDATE touches zero
            # rows and the dispatcher must NOT run the job (its
            # status is now cancelled / paused, and the loaded
            # ``job`` object still says queued). Without this, the
            # claim overwrites the cancel and the job runs even
            # though the user cancelled it.
            claimed = (
                session.query(Job)
                .filter_by(id=job_id, status=JobStatus.QUEUED.value)
                .update(
                    {"status": JobStatus.RUNNING.value},
                    synchronize_session=False,
                )
            )
            session.commit()
            if not claimed:
                return
            self._active_job_id = job_id
            with self._lock:
                self._events.setdefault(job_id, threading.Event()).clear()
        finally:
            session.close()

        control = get_control(job_id)
        run_session = get_session()
        try:
            # Resolve the data dir fresh unless the queue was built
            # with an explicit one (see __init__): the runner must
            # write into the directory that is current now.
            run_dir = self.data_dir if self._explicit_data_dir else _data_dir(None)
            self._runner(run_session, job_id, data_dir=run_dir, control=control)
        except Exception:
            # The runner records failures on the job itself; a runner
            # that raises outright (bug, not a job failure) must still
            # release the queue — otherwise every later job stalls.
            import traceback

            traceback.print_exc()
            try:
                failed = run_session.get(Job, job_id)
                if failed is not None and failed.status in ACTIVE_JOB_STATUSES:
                    failed.status = JobStatus.FAILED.value
                    failed.error = (
                        "The worker stopped unexpectedly. Retry the job — "
                        "if it keeps failing, check the run log."
                    )
                    run_session.commit()
            except Exception:
                pass
        finally:
            drop_control(job_id)
            run_session.close()
            self._active_job_id = None
            with self._lock:
                event = self._events.get(job_id)
            if event is not None:
                event.set()
            with self._condition:
                self._condition.notify_all()


# One shared queue per process (the API routers use this).
_default_queue: JobQueue | None = None
_default_lock = threading.Lock()


def get_job_queue(data_dir: str | Path | None = None) -> JobQueue:
    """The shared process-wide job queue (created on first use)."""
    global _default_queue
    with _default_lock:
        if _default_queue is None:
            _default_queue = JobQueue(data_dir=data_dir)
        return _default_queue


def reset_job_queue() -> None:
    """Drop the shared queue (tests only) — stops its dispatcher."""
    global _default_queue
    with _default_lock:
        if _default_queue is not None:
            _default_queue.shutdown()
        _default_queue = None


__all__ = [
    "CANCELLABLE_STATUSES",
    "ENQUEUEABLE_STATUSES",
    "JobQueue",
    "get_job_queue",
    "reset_job_queue",
]
