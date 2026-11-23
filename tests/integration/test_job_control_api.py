"""v0.9.0 job control + notifications API tests (Worker C).

Covers the HTTP contract (docs/API-CONTRACT.md §2/§6): pause /
resume / cancel endpoints, async run (``wait=false``) with an honest
queue position, the worker-queue shape, and the notifications
endpoints (both the ``/api/notifications`` contract and the
``/notifications`` bell aliases). Queue ordering itself is proved
deterministically in tests/unit/test_job_queue.py with a fake runner;
here the assertions are race-tolerant (the dispatcher runs live).
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="FastAPI is not installed")
from fastapi.testclient import TestClient  # noqa: E402

try:
    from clipforge_api.app import app
except ImportError:
    pytest.skip(
        "clipforge_api.app not implemented yet — see docs/api.md",
        allow_module_level=True,
    )

client = TestClient(app)


def _create_job(source_path: str = "/tmp/clipforge-does-not-exist-control.mp4") -> str:
    resp = client.post(
        "/jobs",
        json={
            "source_type": "local",
            "source_path": source_path,
            "title": "Control test",
            "own_content_declaration": True,
            "transcript_text": "Hello world this is a test.",
            "top_n": 1,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _cleanup(job_id: str) -> None:
    client.post(f"/jobs/{job_id}/cancel")


def _seed_queued_job() -> str:
    """A queued job seeded directly in the DB — POST /jobs auto-runs
    (v0.9.0), so a queued job created via the API would already be
    running by the time the test pauses it."""
    from clipforge.db import Job, JobStatus, get_session

    session = get_session()
    try:
        job = Job(
            source_type="local",
            source_path="/tmp/clipforge-does-not-exist-control.mp4",
            title="Control test",
            status=JobStatus.QUEUED.value,
        )
        session.add(job)
        session.commit()
        return job.id
    finally:
        session.close()


# ------------------------------------------------------------------ job control
def test_pause_and_resume_queued_job():
    job_id = _seed_queued_job()
    try:
        paused = client.post(f"/jobs/{job_id}/pause")
        assert paused.status_code == 200, paused.text
        assert paused.json()["status"] == "paused"
        # Resume on a non-paused job is refused; pause on a finished job too.
        resumed = client.post(f"/jobs/{job_id}/resume")
        assert resumed.status_code == 200, resumed.text
        # Queued again — or already claimed by the dispatcher (it wakes
        # on resume); either way the job is going, not stuck paused.
        assert resumed.json()["status"] in {"queued", "running"}
        assert client.post(f"/jobs/{job_id}/resume").status_code == 409
    finally:
        _cleanup(job_id)


def test_pause_refused_for_finished_job():
    job_id = _create_job()
    try:
        # Run it (missing file → failed, fast) then pause is refused.
        assert client.post(f"/jobs/{job_id}/run").status_code == 200
        assert client.get(f"/jobs/{job_id}").json()["status"] == "failed"
        assert client.post(f"/jobs/{job_id}/pause").status_code == 409
        assert client.post(f"/jobs/{job_id}/resume").status_code == 409
    finally:
        _cleanup(job_id)


def test_run_wait_false_queues_without_blocking():
    job_id = _create_job()
    try:
        resp = client.post(f"/jobs/{job_id}/run", params={"wait": False})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Either still queued (honest position) or already claimed by
        # the dispatcher (running) — never a fake "done".
        assert body["status"] in {"queued", "running"}
        assert body.get("queue_position") is None or body["queue_position"] >= 1
    finally:
        _cleanup(job_id)


def test_cancel_queued_job_is_immediate():
    # Seeded directly: POST /jobs auto-runs (v0.9.0), so a queued job
    # created via the API would already be running when cancel lands
    # and the cancel would be async (cancelling: true) instead.
    job_id = _seed_queued_job()
    resp = client.post(f"/jobs/{job_id}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["cancelling"] is False
    assert client.post(f"/jobs/{job_id}/cancel").status_code == 409


def test_worker_queue_shape():
    body = client.get("/worker/queue").json()
    assert set(body) >= {"running", "queued", "paused", "next", "recent_runs", "active_job_id"}
    assert isinstance(body["queued"], list)
    assert isinstance(body["paused"], list)


def test_failed_job_creates_error_notification():
    job_id = _create_job()
    run = client.post(f"/jobs/{job_id}/run")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "failed"
    # Plain language: no traceback, no "ClassName:" prefix.
    assert body["error"]
    assert "Traceback" not in body["error"]
    assert not body["error"].startswith("IngestError")
    assert body["error_title"]
    assert body["error_action"]
    # The failure is a notification the bell can show.
    feed = client.get("/notifications").json()
    assert isinstance(feed, list)
    mine = [n for n in feed if n.get("link") == f"/jobs/{job_id}"]
    assert mine, "a failed job must create a notification linked to the job"
    assert mine[0]["tone"] == "danger"
    assert mine[0]["title"] == "Job failed"


# ------------------------------------------------------------------ notifications
def test_notifications_api_contract():
    created = client.post(
        "/api/notifications",
        json={
            "level": "warning",
            "title": "Control test notification",
            "message": "test message",
            "action": "/settings",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["level"] == "warning"
    assert body["action"] == "/settings"
    note_id = body["id"]

    listed = client.get("/api/notifications", params={"unread_only": True}).json()
    assert listed["unread"] >= 1
    assert any(n["id"] == note_id for n in listed["notifications"])

    read = client.post(f"/api/notifications/{note_id}/read")
    assert read.status_code == 200
    assert read.json()["read"] is True
    assert client.post("/api/notifications/does-not-exist/read").status_code == 404

    bad = client.post("/api/notifications", json={"level": "info", "title": "  "})
    assert bad.status_code == 422


def test_notifications_ui_aliases_and_clear():
    client.post(
        "/api/notifications",
        json={"level": "success", "title": "Alias test", "message": "ok"},
    )
    feed = client.get("/notifications").json()
    assert isinstance(feed, list)
    mine = [n for n in feed if n["title"] == "Alias test"]
    assert mine and mine[0]["tone"] == "success"
    assert mine[0]["body"] == "ok"

    read = client.post("/notifications/read", json={})
    assert read.status_code == 200
    assert read.json()["read"] >= 1

    cleared = client.post("/api/notifications/clear")
    assert cleared.status_code == 200
    assert cleared.json()["cleared"] >= 1
    assert client.get("/api/notifications").json()["unread"] == 0

    read_all = client.post("/api/notifications/read-all")
    assert read_all.status_code == 200
    assert read_all.json()["marked_read"] == 0
