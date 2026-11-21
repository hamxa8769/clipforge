"""Tests for ``clipforge.batch_queue`` (v0.8.0, Worker A)."""

from __future__ import annotations

import json
import threading
import time

import pytest

from clipforge import perf
from clipforge.batch_queue import BatchQueue


def fast_processor(item, set_progress, is_paused):
    """Instant success — used where timing does not matter."""
    set_progress(100)


@pytest.fixture
def queue(tmp_path):
    q = BatchQueue(data_dir=tmp_path, processor=fast_processor)
    yield q
    q.shutdown()


def test_create_and_complete_batch(queue):
    batch_id = queue.create_batch(["a.mp4", "b.mp4"])
    batch = queue.wait_until_done(batch_id, timeout=10.0)
    assert batch is not None
    assert batch["status"] == "done"
    assert batch["total"] == 2
    assert batch["done"] == 2
    assert all(item["state"] == "done" for item in batch["items"])
    assert all(item["progress"] == 100 for item in batch["items"])


def test_create_batch_is_non_blocking(tmp_path):
    started = threading.Event()
    released = threading.Event()

    def blocking_processor(item, set_progress, is_paused):
        started.set()
        released.wait(timeout=10)

    q = BatchQueue(data_dir=tmp_path, processor=blocking_processor)
    try:
        t0 = time.time()
        batch_id = q.create_batch(["slow.mp4"])
        assert time.time() - t0 < 1.0  # returned while processing runs
        assert started.wait(timeout=5.0)
        assert q.get_batch(batch_id)["items"][0]["state"] == "processing"
    finally:
        released.set()
        q.shutdown()


def test_create_batch_rejects_empty_sources(queue):
    with pytest.raises(ValueError):
        queue.create_batch([])
    with pytest.raises(ValueError):
        queue.create_batch(["   "])


def test_get_unknown_batch_returns_none(queue):
    assert queue.get_batch("nope") is None
    assert queue.pause_batch("nope") is None
    assert queue.resume_batch("nope") is None
    assert queue.cancel_batch("nope") is None


# ------------------------------------------------------- v0.8.1 batch fix
def test_get_queue_adopts_real_processor_over_simulated(tmp_path):
    """A read before the first write must not poison the shared queue
    with the simulated processor: a GET /batch/{id} (404 or not) used
    to create the process-wide queue with the no-op processor, and
    every later POST /batch silently ran the simulation — fake "done",
    no job, no clips. The real processor is adopted over the simulated
    one; the shared instance itself is unchanged."""
    from clipforge import batch_queue as bq_mod

    calls: list[str] = []

    def _real(item, set_progress, is_paused):
        calls.append(item["source"])
        return {"job_id": "j1", "clips": 0, "status": "done"}

    orig = bq_mod._default_queue
    bq_mod._default_queue = None
    try:
        q1 = bq_mod.get_queue(tmp_path)  # read-only caller: simulated
        assert q1._processor is bq_mod._default_processor
        q2 = bq_mod.get_queue(tmp_path, processor=_real)
        assert q2 is q1
        assert q2._processor is _real
        batch_id = q2.create_batch(["a.mp4"])
        done = q2.wait_until_done(batch_id, timeout=10.0)
        assert done is not None
        assert calls == ["a.mp4"]
        assert done["items"][0]["result"] == {"job_id": "j1", "clips": 0, "status": "done"}
        assert done["items"][0]["state"] == "done"
    finally:
        if bq_mod._default_queue is not None:
            bq_mod._default_queue.shutdown()
        bq_mod._default_queue = orig


def test_blocked_pipeline_result_marks_item_failed(tmp_path):
    """A processor result with status blocked/failed is a failed item
    carrying the plain reason — never a fake "done" with zero clips."""

    def _blocked(item, set_progress, is_paused):
        return {
            "job_id": "j1",
            "clips": 0,
            "status": "blocked",
            "error": "faster-whisper or OPENAI_API_KEY missing (transcription_backend_missing)",
        }

    q = BatchQueue(data_dir=tmp_path, processor=_blocked)
    try:
        batch_id = q.create_batch(["a.mp4"])
        done = q.wait_until_done(batch_id, timeout=10.0)
        assert done is not None
        item = done["items"][0]
        assert item["state"] == "failed"
        assert "transcription" in item["error"]
        assert item["result"]["status"] == "blocked"
        assert done["status"] == "partial"
    finally:
        q.shutdown()


def test_pause_and_resume(tmp_path):
    gate = threading.Event()

    def gated_processor(item, set_progress, is_paused):
        gate.wait(timeout=10)
        if is_paused():
            return
        set_progress(100)

    q = BatchQueue(data_dir=tmp_path, processor=gated_processor)
    try:
        batch_id = q.create_batch(["one.mp4", "two.mp4"])
        # Wait until the first item is in flight, then pause.
        deadline = time.time() + 5
        while time.time() < deadline:
            batch = q.get_batch(batch_id)
            if batch["items"][0]["state"] == "processing":
                break
            time.sleep(0.02)
        q.pause_batch(batch_id)
        gate.set()  # let the in-flight item finish its step
        time.sleep(0.3)
        batch = q.get_batch(batch_id)
        assert batch["items"][0]["state"] == "paused"
        assert batch["items"][1]["state"] == "queued"  # never started
        assert batch["status"] == "paused"
        resumed = q.resume_batch(batch_id)
        assert resumed["items"][0]["state"] == "queued"
        done = q.wait_until_done(batch_id, timeout=10.0)
        assert done["status"] == "done"
    finally:
        gate.set()
        q.shutdown()


def test_cancel_marks_queued_items_failed(tmp_path):
    gate = threading.Event()

    def gated_processor(item, set_progress, is_paused):
        gate.wait(timeout=10)
        set_progress(100)

    q = BatchQueue(data_dir=tmp_path, processor=gated_processor)
    try:
        batch_id = q.create_batch(["one.mp4", "two.mp4", "three.mp4"])
        deadline = time.time() + 5
        while time.time() < deadline:
            if q.get_batch(batch_id)["items"][0]["state"] == "processing":
                break
            time.sleep(0.02)
        q.cancel_batch(batch_id)
        gate.set()
        done = q.wait_until_done(batch_id, timeout=10.0)
        assert done["status"] == "partial"
        assert all(item["state"] == "failed" for item in done["items"])
        assert all(item["error"] == "cancelled" for item in done["items"])
    finally:
        gate.set()
        q.shutdown()


def test_processor_exception_marks_item_failed_not_queue_dead(tmp_path):
    def flaky(item, set_progress, is_paused):
        if item["source"] == "bad.mp4":
            raise RuntimeError("boom")
        set_progress(100)

    q = BatchQueue(data_dir=tmp_path, processor=flaky)
    try:
        batch_id = q.create_batch(["bad.mp4", "good.mp4"])
        done = q.wait_until_done(batch_id, timeout=10.0)
        by_source = {i["source"]: i for i in done["items"]}
        assert by_source["bad.mp4"]["state"] == "failed"
        assert "boom" in by_source["bad.mp4"]["error"]
        assert "Traceback" not in by_source["bad.mp4"]["error"]
        assert by_source["good.mp4"]["state"] == "done"
    finally:
        q.shutdown()


def test_concurrency_cap_from_low_profile(tmp_path):
    perf.save_override("low", tmp_path)
    active = {"n": 0, "max": 0}
    lock = threading.Lock()

    def slow_processor(item, set_progress, is_paused):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.2)
        with lock:
            active["n"] -= 1
        set_progress(100)

    q = BatchQueue(data_dir=tmp_path, processor=slow_processor)
    try:
        assert q.max_concurrent() == 1  # low profile: one at a time
        batch_id = q.create_batch(["a.mp4", "b.mp4", "c.mp4", "d.mp4"])
        done = q.wait_until_done(batch_id, timeout=15.0)
        assert done["status"] == "done"
        assert active["max"] == 1
    finally:
        q.shutdown()


def test_concurrency_grows_on_high_profile(tmp_path):
    perf.save_override("high", tmp_path)
    active = {"n": 0, "max": 0}
    lock = threading.Lock()

    def slow_processor(item, set_progress, is_paused):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.3)
        with lock:
            active["n"] -= 1
        set_progress(100)

    q = BatchQueue(data_dir=tmp_path, processor=slow_processor)
    try:
        assert q.max_concurrent() == 4
        batch_id = q.create_batch(["a.mp4", "b.mp4", "c.mp4", "d.mp4"])
        done = q.wait_until_done(batch_id, timeout=15.0)
        assert done["status"] == "done"
        assert active["max"] >= 2  # parallel on a strong machine
    finally:
        q.shutdown()


def test_persistence_across_instances(tmp_path):
    q1 = BatchQueue(data_dir=tmp_path, processor=fast_processor)
    batch_id = q1.create_batch(["a.mp4", "b.mp4"])
    done = q1.wait_until_done(batch_id, timeout=10.0)
    assert done["status"] == "done"
    q1.shutdown()
    assert (tmp_path / "batches.json").exists()
    raw = json.loads((tmp_path / "batches.json").read_text(encoding="utf-8"))
    assert batch_id in raw["batches"]

    q2 = BatchQueue(data_dir=tmp_path, processor=fast_processor)
    try:
        loaded = q2.get_batch(batch_id)
        assert loaded is not None
        assert loaded["status"] == "done"
        assert loaded["done"] == 2
    finally:
        q2.shutdown()


def test_progress_reported_0_to_100(tmp_path):
    seen = []

    def staged(item, set_progress, is_paused):
        for pct in (10, 50, 90):
            set_progress(pct)
            seen.append(pct)
            time.sleep(0.02)

    q = BatchQueue(data_dir=tmp_path, processor=staged)
    try:
        batch_id = q.create_batch(["a.mp4"])
        done = q.wait_until_done(batch_id, timeout=10.0)
        assert done["items"][0]["progress"] == 100
        assert seen == [10, 50, 90]
    finally:
        q.shutdown()
