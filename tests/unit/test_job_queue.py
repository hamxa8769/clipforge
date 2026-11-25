"""v0.9.0 job queue engine tests (Worker C).

The real queue: one active job at a time, FIFO with priority for
user-started runs, pause / resume / cancel that work, restart-safe
reconciliation (no duplicate run after a restart), and a failure that
stays failed with a plain-language error + a notification.

A fake runner stands in for the pipeline: it records start order,
blocks on an event (so the test can hold a job "running"), and
honours the job's control object exactly like the real worker
(``wait_if_paused`` between steps, ``JobCancelled`` on cancel).
"""

from __future__ import annotations

import threading
import time

import pytest

from clipforge.db import Job, JobStatus, Notification, WorkerRun, get_session, init_db
from clipforge.errors import JobCancelled
from clipforge.job_control import get_control
from clipforge.job_queue import JobQueue, reset_job_queue
from clipforge.worker import ACTIVE_JOB_STATUSES

# The unit tests never import the API app (which creates the tables
# at import time), so create them here — idempotent.
init_db()


def _make_job(session, status: str = "pending", **meta) -> str:
    job = Job(
        source_type="local",
        source_path="/tmp/does-not-matter.mp4",
        title=f"Queue test {status}",
        status=status,
        meta_json={"top_n": 3, **meta},
    )
    session.add(job)
    session.commit()
    return job.id


def _get_job(job_id: str) -> Job:
    session = get_session()
    try:
        return session.get(Job, job_id)
    finally:
        session.close()


def _wait_for(predicate, timeout: float = 10.0, what: str = "condition") -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _position(q: JobQueue, job_id: str) -> int | None:
    """Queue position for a job, closing the session it reads with."""
    session = get_session()
    try:
        return q.queue_position(session, job_id)
    finally:
        session.close()


class FakeRunner:
    """Stand-in pipeline: blocks until released, honours pause/cancel."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.finished: list[str] = []
        self._release = threading.Event()
        self._started_events: dict[str, threading.Event] = {}

    def release(self) -> None:
        self._release.set()

    def wait_started(self, job_id: str, timeout: float = 10.0) -> None:
        event = self._started_events.setdefault(job_id, threading.Event())
        assert event.wait(timeout), f"job {job_id} never started"

    def __call__(self, session, job_id, data_dir=None, control=None):
        control = control or get_control(job_id)
        self.started.append(job_id)
        self._started_events.setdefault(job_id, threading.Event()).set()
        try:
            # Two "stages": pause / cancel land between them. The
            # blocking wait polls the cancel flag so a cancel lands
            # at once instead of after the 30 s timeout.
            control.wait_if_paused()
            deadline = time.time() + 30
            while not self._release.is_set() and time.time() < deadline:
                control.check_cancelled()
                self._release.wait(timeout=0.1)
            control.wait_if_paused()
            control.check_cancelled()
        except JobCancelled:
            job = session.get(Job, job_id)
            job.status = JobStatus.CANCELLED.value
            job.error = "Job cancelled by the user."
            session.commit()
            raise
        job = session.get(Job, job_id)
        job.status = JobStatus.REVIEW.value
        session.commit()
        self.finished.append(job_id)
        return {"job_id": job_id, "status": job.status}


def _queue_drained(q: JobQueue) -> bool:
    """True when the dispatcher is idle and nothing is waiting."""
    if q._active_job_id is not None:
        return False
    session = get_session()
    try:
        return (
            session.query(Job)
            .filter(Job.status.in_([JobStatus.QUEUED.value, JobStatus.PENDING.value]))
            .count()
            == 0
        )
    finally:
        session.close()


@pytest.fixture
def queue(tmp_path):
    # Kill the process-wide singleton queue first: API tests earlier
    # in the same pytest process leave its dispatcher alive, and it
    # would claim this test's jobs and run the real pipeline on them
    # (the fake runner would never see them).
    reset_job_queue()
    # Clean slate: integration tests earlier in the same process leave
    # jobs behind — v0.9.0 auto-run enqueues every created job, so a
    # leftover queued / pending job would shift queue positions and
    # could be claimed by this test's dispatcher. Cancel them all up
    # front (test DB only); terminal jobs are left alone.
    _session = get_session()
    try:
        _session.query(Job).filter(
            Job.status.in_(
                [
                    JobStatus.PENDING.value,
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                    JobStatus.PAUSED.value,
                    JobStatus.TRANSCRIBING.value,
                    JobStatus.SCORING.value,
                    JobStatus.RENDERING.value,
                ]
            )
        ).update(
            {
                "status": JobStatus.CANCELLED.value,
                "error": "Cancelled by test setup.",
            },
            synchronize_session=False,
        )
        _session.commit()
    finally:
        _session.close()
    runner = FakeRunner()
    q = JobQueue(data_dir=tmp_path / "data", runner=runner)
    yield q, runner
    # Drain: a job left running by the test must finish (or the next
    # test's queue would reconcile it and run it again under a new
    # runner). Release unblocks the fake runner; the dispatcher then
    # clears the active job within a moment.
    #
    # Waiting only for ``_active_job_id is None`` is NOT enough: it
    # passes instantly while a job is still QUEUED (the dispatcher
    # polls every 0.2 s and has not claimed it yet). The next test's
    # dispatcher then claims that leftover first and the test's own
    # jobs starve ("job never started") — a flaky cascade. So wait
    # until nothing is active AND nothing is queued, and cancel any
    # job asleep in a pause checkpoint first (release alone cannot
    # wake it). Leftovers still around afterwards are cancelled
    # outright, so the next queue's reconcile cannot re-queue them.
    runner.release()
    session = get_session()
    try:
        stuck = (
            session.query(Job)
            .filter(Job.status.in_([JobStatus.PAUSED.value, *sorted(ACTIVE_JOB_STATUSES)]))
            .all()
        )
        for job in stuck:
            get_control(job.id).request_cancel()
    finally:
        session.close()
    try:
        _wait_for(lambda: _queue_drained(q), timeout=15, what="queue to drain")
    finally:
        session = get_session()
        try:
            session.query(Job).filter(
                Job.status.in_(
                    [
                        JobStatus.QUEUED.value,
                        JobStatus.PENDING.value,
                        JobStatus.PAUSED.value,
                    ]
                )
            ).update(
                {
                    "status": JobStatus.CANCELLED.value,
                    "error": "Cancelled by test cleanup.",
                },
                synchronize_session=False,
            )
            session.commit()
        finally:
            session.close()
        q.shutdown()


def test_second_job_waits_while_first_runs(queue):
    q, runner = queue
    session = get_session()
    try:
        first = _make_job(session)
        second = _make_job(session)
        q.enqueue(session, first, priority=0)
        q.enqueue(session, second, priority=0)
    finally:
        session.close()
    runner.wait_started(first)
    # The second job is honestly queued — never started in parallel.
    # Position counts waiting jobs only (the running job is shown
    # separately as running), so the second job is first in line.
    assert _get_job(second).status == JobStatus.QUEUED.value
    assert _position(q, second) == 1
    assert q._active_job_id == first
    assert len(runner.started) == 1
    runner.release()
    _wait_for(
        lambda: _get_job(second).status == JobStatus.REVIEW.value,
        what="second job to finish",
    )
    assert runner.started == [first, second]
    assert runner.finished == [first, second]


def test_user_started_run_jumps_the_queue(queue):
    q, runner = queue
    session = get_session()
    try:
        blocker = _make_job(session)
        q.enqueue(session, blocker, priority=0)
    finally:
        session.close()
    # The blocker must be RUNNING before the others arrive: the three
    # enqueues below happen within milliseconds, so a dispatcher that
    # wakes after all three are queued would (correctly) pick the
    # priority-1 user job first and the blocker would never start
    # until it finished.
    runner.wait_started(blocker)
    session = get_session()
    try:
        batch_job = _make_job(session)
        user_job = _make_job(session)
        q.enqueue(session, batch_job, priority=0)
        q.enqueue(session, user_job, priority=1)
    finally:
        session.close()
    # User-started runs first: position 1 of the waiting jobs.
    assert _position(q, user_job) == 1
    assert _position(q, batch_job) == 2
    runner.release()
    _wait_for(
        lambda: _get_job(batch_job).status == JobStatus.REVIEW.value,
        what="both queued jobs to finish",
    )
    assert runner.started == [blocker, user_job, batch_job]


def test_enqueue_is_idempotent(queue):
    q, _runner = queue
    session = get_session()
    try:
        job_id = _make_job(session)
        first = q.enqueue(session, job_id, priority=0)
        try:
            second = q.enqueue(session, job_id, priority=1)
            if second["queue_position"] is None:
                # Same dispatcher race as the ValueError below, one
                # step later: enqueue's conditional UPDATE touched
                # zero rows (claimed in the meantime), the re-read
                # still saw the job queued, and the dispatcher
                # committed the claim before the queue_position
                # query — so the report says "queued" with no
                # position. Still one run, never two.
                assert _get_job(job_id).status in {
                    JobStatus.RUNNING.value,
                    JobStatus.REVIEW.value,
                }
            else:
                assert first["queue_position"] == second["queue_position"] == 1
        except ValueError:
            # The dispatcher wakes on enqueue and may claim the job
            # between the two calls — still one run, never two.
            assert _get_job(job_id).status == JobStatus.RUNNING.value
        # Idempotent: still one job, queued or already claimed by the
        # dispatcher (which is fast) — never two runs.
        assert _get_job(job_id).status in {
            JobStatus.QUEUED.value,
            JobStatus.RUNNING.value,
        }
    finally:
        session.close()


def test_pause_queued_job_skips_it_until_resumed(queue):
    q, runner = queue
    session = get_session()
    try:
        first = _make_job(session)
        second = _make_job(session)
        q.enqueue(session, first, priority=0)
        q.enqueue(session, second, priority=0)
        q.pause(session, second)
        assert _get_job(second).status == JobStatus.PAUSED.value
    finally:
        session.close()
    runner.wait_started(first)
    runner.release()
    _wait_for(
        lambda: _get_job(first).status == JobStatus.REVIEW.value,
        what="first job to finish",
    )
    # Paused job never started while paused — even with an empty queue.
    time.sleep(0.5)
    assert runner.started == [first]
    assert _get_job(second).status == JobStatus.PAUSED.value
    session = get_session()
    try:
        q.resume(session, second)
        # Queued again — or already claimed by the dispatcher (it wakes
        # on resume); either way the job is going, not stuck paused.
        assert _get_job(second).status in {
            JobStatus.QUEUED.value,
            JobStatus.RUNNING.value,
        }
    finally:
        session.close()
    _wait_for(
        lambda: _get_job(second).status == JobStatus.REVIEW.value,
        what="resumed job to finish",
    )
    assert runner.started == [first, second]


def test_pause_running_job_then_resume(queue):
    q, runner = queue
    session = get_session()
    try:
        job_id = _make_job(session)
        q.enqueue(session, job_id, priority=0)
    finally:
        session.close()
    runner.wait_started(job_id)
    session = get_session()
    try:
        q.pause(session, job_id)
    finally:
        session.close()
    assert _get_job(job_id).status == JobStatus.PAUSED.value
    runner.release()
    # Paused mid-run: the runner is stuck at its pause checkpoint.
    time.sleep(0.5)
    assert job_id not in runner.finished
    session = get_session()
    try:
        result = q.resume(session, job_id)
        assert result["status"] == JobStatus.RUNNING.value
    finally:
        session.close()
    _wait_for(
        lambda: _get_job(job_id).status == JobStatus.REVIEW.value,
        what="resumed job to finish",
    )
    assert runner.finished == [job_id]


def test_cancel_running_job(queue):
    q, runner = queue
    session = get_session()
    try:
        job_id = _make_job(session)
        q.enqueue(session, job_id, priority=0)
    finally:
        session.close()
    runner.wait_started(job_id)
    session = get_session()
    try:
        result = q.cancel(session, job_id)
        assert result["status"] == "cancelling"
    finally:
        session.close()
    _wait_for(
        lambda: _get_job(job_id).status == JobStatus.CANCELLED.value,
        what="job to cancel",
    )
    assert job_id not in runner.finished


def test_cancel_queued_job_is_immediate(queue):
    q, _runner = queue
    session = get_session()
    try:
        job_id = _make_job(session)
        q.enqueue(session, job_id, priority=0)
        result = q.cancel(session, job_id)
        # Usually immediate; if the dispatcher claimed the job in the
        # meantime it reports "cancelling" and the worker confirms it.
        assert result["status"] in {JobStatus.CANCELLED.value, "cancelling"}
    finally:
        session.close()
    _wait_for(
        lambda: _get_job(job_id).status == JobStatus.CANCELLED.value,
        what="job to cancel",
    )


def test_claim_loses_to_cancel_after_select(queue, monkeypatch):
    """A cancel that lands after the dispatcher selected the job but
    before it claimed it must win: the job stays cancelled and the
    runner never starts it. (Lost-update race: the claim used to
    overwrite the cancel with RUNNING.)"""
    q, runner = queue
    reset_job_queue()
    q.shutdown()
    q._dispatcher.join(timeout=2)
    session = get_session()
    try:
        job_id = _make_job(session)
        q.enqueue(session, job_id, priority=0)
    finally:
        session.close()

    orig_next = q._next_queued

    def _racing_next(s):
        job = orig_next(s)
        if job is not None:
            other = get_session()
            try:
                q.cancel(other, job.id)
            finally:
                other.close()
        return job

    monkeypatch.setattr(q, "_next_queued", _racing_next)
    q._dispatch_once()
    assert _get_job(job_id).status == JobStatus.CANCELLED.value
    assert runner.started == []


def test_two_dispatchers_run_a_job_once(tmp_path):
    """Two queues against one DB (the test-process shape): the atomic
    claim lets exactly one dispatcher own the job — never two runs."""
    reset_job_queue()
    runner = FakeRunner()
    q1 = JobQueue(data_dir=tmp_path / "d1", runner=runner)
    q2 = JobQueue(data_dir=tmp_path / "d2", runner=runner)
    try:
        session = get_session()
        try:
            job_id = _make_job(session)
            q1.enqueue(session, job_id, priority=0)
        finally:
            session.close()
        runner.release()
        _wait_for(
            lambda: _get_job(job_id).status == JobStatus.REVIEW.value,
            what="job to finish",
        )
        assert runner.started.count(job_id) == 1
    finally:
        q1.shutdown()
        q2.shutdown()


def test_queue_follows_current_data_dir(tmp_path, monkeypatch):
    """A queue created without an explicit dir follows
    CLIPFORGE_DATA_DIR as it is at RUN time, not creation time: the
    API singleton is created once per process, but tests re-point the
    env var per module — pinning the first module's dir would write
    the next module's job artifacts into the wrong directory."""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(dir_a))
    reset_job_queue()
    seen: list[str] = []

    def _recorder(session, job_id, data_dir=None, control=None):
        seen.append(str(data_dir))
        job = session.get(Job, job_id)
        job.status = JobStatus.REVIEW.value
        session.commit()
        return {"job_id": job_id, "status": job.status}

    q = JobQueue(runner=_recorder)
    try:
        monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(dir_b))
        session = get_session()
        try:
            job_id = _make_job(session)
            q.enqueue(session, job_id, priority=0)
        finally:
            session.close()
        _wait_for(
            lambda: _get_job(job_id).status == JobStatus.REVIEW.value,
            what="job to finish",
        )
        assert seen == [str(dir_b)]
    finally:
        q.shutdown()


def test_reconcile_requeues_interrupted_jobs_without_duplicate(tmp_path):
    # The dispatcher is stopped for the reconcile assertions: with it
    # running it could claim the re-queued job between the two
    # reconciles and the test would be timing-dependent.
    reset_job_queue()
    runner = FakeRunner()
    q = JobQueue(data_dir=tmp_path / "data", runner=runner)
    q.shutdown()
    q._dispatcher.join(timeout=2)
    try:
        session = get_session()
        try:
            job_id = _make_job(session, status=JobStatus.RUNNING.value)
            run = WorkerRun(job_id=job_id, status="running", outcome="started")
            session.add(run)
            session.commit()
            requeued = q.reconcile(session)
            assert requeued == [job_id]
            assert _get_job(job_id).status == JobStatus.QUEUED.value
            session.refresh(run)
            assert run.status == "interrupted"
            # A notification names the restart — nothing fails silently.
            note = (
                session.query(Notification)
                .filter_by(job_id=job_id)
                .order_by(Notification.created_at.desc())
                .first()
            )
            assert note is not None and note.level == "warning"
            # Second reconcile: nothing left to re-queue, no duplicate.
            assert q.reconcile(session) == []
        finally:
            session.close()
    finally:
        q.shutdown()
    # The re-queued job runs exactly once under a fresh dispatcher.
    q2 = JobQueue(data_dir=tmp_path / "data", runner=runner)
    try:
        runner.release()
        _wait_for(
            lambda: _get_job(job_id).status == JobStatus.REVIEW.value,
            what="re-queued job to finish",
        )
        assert runner.started.count(job_id) == 1
    finally:
        q2.shutdown()


def test_failed_job_keeps_plain_language_error_and_notifies(tmp_path):
    """Real pipeline, missing source file: failed + plain error + notification."""
    from clipforge.worker import run_job

    reset_job_queue()
    q = JobQueue(data_dir=tmp_path / "data", runner=run_job)
    try:
        session = get_session()
        try:
            job_id = _make_job(session)
            session.query(Job).filter_by(id=job_id).update(
                {"source_path": "/tmp/clipforge-does-not-exist-queue.mp4"}
            )
            session.commit()
        finally:
            session.close()
        summary = q.enqueue_and_wait(get_session(), job_id, priority=1)
        assert summary["status"] == JobStatus.FAILED.value
        job = _get_job(job_id)
        assert job.error
        assert "Traceback" not in job.error
        assert not job.error.startswith(("IngestError", "FileNotFoundError", "RuntimeError"))
        session = get_session()
        try:
            note = (
                session.query(Notification)
                .filter_by(job_id=job_id)
                .order_by(Notification.created_at.desc())
                .first()
            )
            assert note is not None
            assert note.level == "error"
            assert note.action == f"/jobs/{job_id}"
        finally:
            session.close()
    finally:
        q.shutdown()
