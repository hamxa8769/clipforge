"""API integration tests — live clipping router (v0.7.0, Worker C).

Contract (router mounted standalone here, like test_trending_api.py):

  GET  /api/trending/live?niche=&licensed_only=&max_results=
  GET  /api/trending/live/mine
  POST /api/trending/live/capture {video_url|video_id, duration_s,
       niche, top_n, rights_confirmed}   -> 202, job runs the worker
  POST /api/trending/live/{job_id}/cancel

YouTube HTTP is mocked by monkeypatching ``httpx.Client`` in the
router module — no test ever hits the real YouTube API. The worker is
monkeypatched per capture test (a fake that marks the job reviewed)
except where the test asserts the endpoint's own validation. Every
error asserted here is plain language.
"""

from __future__ import annotations

import time

import httpx
import pytest
from clipforge_api import trending as trending_router  # noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()
app.include_router(trending_router.router)
client = TestClient(app)

LIVE_ID_CC = "L1v3Str34mA"
LIVE_ID_STD = "L1v3Str34mB"
LIVE_URL_CC = f"https://www.youtube.com/watch?v={LIVE_ID_CC}"
LIVE_URL_STD = f"https://www.youtube.com/watch?v={LIVE_ID_STD}"
OTHER_CHANNEL = "UCBBBBBBBBB"


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Each test gets its own data dir (agent.json + secrets live there)."""
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _clean_live_rows():
    """Remove exactly the rows this module created (jobs flagged live
    in meta), so later tests see the DB as they left it."""
    from clipforge.db import ClipCandidate, Job, Post, WorkerRun, session_factory  # noqa: PLC0415

    yield
    session = session_factory()()
    try:
        live_jobs = [j for j in session.query(Job).all() if (j.meta_json or {}).get("live")]
        ids = {j.id for j in live_jobs}
        if not ids:
            return
        cand_ids = [
            c.id for c in session.query(ClipCandidate).filter(ClipCandidate.job_id.in_(ids)).all()
        ]
        if cand_ids:
            session.query(Post).filter(Post.clip_candidate_id.in_(cand_ids)).delete(
                synchronize_session=False
            )
        session.query(ClipCandidate).filter(ClipCandidate.job_id.in_(ids)).delete(
            synchronize_session=False
        )
        session.query(WorkerRun).filter(WorkerRun.job_id.in_(ids)).delete(synchronize_session=False)
        session.query(Job).filter(Job.id.in_(ids)).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def _fake_youtube_client(seen: dict | None = None):
    seen = seen if seen is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search"):
            seen["search_params"] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": {"videoId": LIVE_ID_CC},
                            "snippet": {"title": "CC live", "channelTitle": "CC Chan"},
                        },
                        {
                            "id": {"videoId": LIVE_ID_STD},
                            "snippet": {"title": "Std live", "channelTitle": "Std Chan"},
                        },
                    ]
                },
            )
        if path.endswith("/videos"):
            seen["videos_part"] = request.url.params.get("part")
            wanted = set((request.url.params.get("id") or "").split(","))
            items = []
            for video_id, licence, title, channel in [
                (LIVE_ID_CC, "creativeCommon", "CC live", "CC Chan"),
                (LIVE_ID_STD, "youtube", "Std live", "Std Chan"),
            ]:
                if video_id not in wanted:
                    continue
                items.append(
                    {
                        "id": video_id,
                        "snippet": {
                            "title": title,
                            "channelTitle": channel,
                            "channelId": OTHER_CHANNEL,
                            "publishedAt": "2026-11-21T10:00:00Z",
                        },
                        "status": {"license": licence},
                        "statistics": {"viewCount": "5000"},
                        "liveStreamingDetails": {
                            "concurrentViewers": "1234",
                            "actualStartTime": "2026-11-21T09:30:00Z",
                        },
                    }
                )
            return httpx.Response(200, json={"items": items})
        if path.endswith("/liveBroadcasts"):
            return httpx.Response(200, json={"items": [{"id": LIVE_ID_CC}]})
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _save_api_key():
    client.post("/api/trending/api-key", json={"api_key": "AIzaFAKEKEY1234"})


def _fake_worker_run(session, job_id, data_dir=None):
    """Stand-in for the worker: the capture already happened (mocked);
    the job just lands in review so the test can assert the record."""
    from clipforge.db import Job, JobStatus  # noqa: PLC0415

    job = session.get(Job, job_id)
    job.status = JobStatus.REVIEW.value
    session.commit()
    return {"job_id": job_id, "status": JobStatus.REVIEW.value}


# ------------------------------------------------------------------ discovery
def test_live_without_credentials_is_plain_422():
    resp = client.get("/api/trending/live", params={"niche": "gaming"})
    assert resp.status_code == 422
    assert "connect your youtube channel" in resp.json()["detail"].lower()


def test_live_with_saved_key_returns_live_streams(monkeypatch):
    _save_api_key()
    fake = _fake_youtube_client()
    monkeypatch.setattr(trending_router.httpx, "Client", lambda **kwargs: fake)
    resp = client.get("/api/trending/live", params={"niche": "gaming"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "api_key"
    assert body["count"] == 2
    assert body["licensed_only"] is False
    cc, std = body["results"]
    assert cc["video_id"] == LIVE_ID_CC
    assert cc["licence"] == "creativeCommon"
    assert cc["can_clip"] is True
    assert cc["viewers"] == 1234
    assert cc["started_at"] == "2026-11-21T09:30:00Z"
    assert std["video_id"] == LIVE_ID_STD
    assert std["licence"] == "youtube"
    # v0.9.1 advisory: the standard-licence stream is reported, never refused.
    assert std["can_clip"] is True
    assert "You are responsible for the rights" in std["clip_reason"]
    assert "You are responsible for the rights" in body["notice"]


def test_live_licensed_only_passes_video_license(monkeypatch):
    _save_api_key()
    seen: dict = {}
    fake = _fake_youtube_client(seen)
    monkeypatch.setattr(trending_router.httpx, "Client", lambda **kwargs: fake)
    resp = client.get("/api/trending/live", params={"niche": "gaming", "licensed_only": True})
    assert resp.status_code == 200
    assert resp.json()["licensed_only"] is True
    assert seen["search_params"]["eventType"] == "live"
    assert seen["search_params"]["videoLicense"] == "creativeCommon"


def test_live_mine_without_connected_channel_is_plain_422(monkeypatch):
    _save_api_key()  # API key alone cannot say which channel is yours
    fake = _fake_youtube_client()
    monkeypatch.setattr(trending_router.httpx, "Client", lambda **kwargs: fake)
    resp = client.get("/api/trending/live/mine")
    assert resp.status_code == 422
    assert "connect your youtube channel" in resp.json()["detail"].lower()


# ------------------------------------------------------------------ capture
def test_capture_requires_rights_confirmation(monkeypatch):
    _save_api_key()
    fake = _fake_youtube_client()
    monkeypatch.setattr(trending_router.httpx, "Client", lambda **kwargs: fake)
    resp = client.post(
        "/api/trending/live/capture",
        json={"video_url": LIVE_URL_CC, "duration_s": 120, "rights_confirmed": False},
    )
    assert resp.status_code == 422
    assert "confirm" in resp.json()["detail"].lower()


def test_capture_rejects_bad_duration(monkeypatch):
    _save_api_key()
    fake = _fake_youtube_client()
    monkeypatch.setattr(trending_router.httpx, "Client", lambda **kwargs: fake)
    resp = client.post(
        "/api/trending/live/capture",
        json={"video_url": LIVE_URL_CC, "duration_s": 30, "rights_confirmed": True},
    )
    assert resp.status_code == 422  # pydantic: 60–300s only


def test_capture_standard_licence_third_party_proceeds(monkeypatch):
    """v0.9.1 ADVISORY: a standard-licence live stream by another
    creator is captured — the licence is re-verified live for an
    accurate record, but the verdict never blocks capture."""
    _save_api_key()
    fake = _fake_youtube_client()
    monkeypatch.setattr(trending_router.httpx, "Client", lambda **kwargs: fake)
    monkeypatch.setattr(trending_router, "worker_run_job", _fake_worker_run)
    resp = client.post(
        "/api/trending/live/capture",
        json={"video_url": LIVE_URL_STD, "duration_s": 120, "rights_confirmed": True},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["video_id"] == LIVE_ID_STD
    assert body["licence"] == "youtube"
    assert body["is_own"] is False


def test_capture_cc_live_starts_live_job(monkeypatch):
    _save_api_key()
    fake = _fake_youtube_client()
    monkeypatch.setattr(trending_router.httpx, "Client", lambda **kwargs: fake)
    monkeypatch.setattr(trending_router, "worker_run_job", _fake_worker_run)
    resp = client.post(
        "/api/trending/live/capture",
        json={
            "video_url": LIVE_URL_CC,
            "duration_s": 120,
            "niche": "gaming",
            "top_n": 1,
            "rights_confirmed": True,
        },
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["video_id"] == LIVE_ID_CC
    assert body["licence"] == "creativeCommon"
    assert body["is_own"] is False
    assert body["duration_s"] == 120
    job_id = body["job_id"]

    # The fake worker lands the job in review; the record carries the
    # licence + the live meta the worker's re-check reads.
    deadline = time.time() + 10
    record = None
    while time.time() < deadline:
        from clipforge.db import Job, session_factory  # noqa: PLC0415

        session = session_factory()()
        try:
            record = session.get(Job, job_id)
            if record is not None and record.status == "review":
                break
        finally:
            session.close()
        time.sleep(0.2)
    assert record is not None
    assert record.source_type == "live"
    assert record.source_url == LIVE_URL_CC
    assert record.own_content is True
    assert record.source_license == "cc-by-3.0"
    assert record.source_title == "CC live"
    assert record.source_channel == "CC Chan"
    meta = record.meta_json or {}
    assert meta.get("live") is True
    assert meta.get("live_licence") == "creativeCommon"
    assert meta.get("live_duration_s") == 120
    assert meta.get("top_n") == 1


def test_capture_unknown_video_is_plain_422(monkeypatch):
    _save_api_key()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    fake = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(trending_router.httpx, "Client", lambda **kwargs: fake)
    resp = client.post(
        "/api/trending/live/capture",
        json={"video_url": LIVE_URL_CC, "duration_s": 120, "rights_confirmed": True},
    )
    assert resp.status_code == 422
    assert "could not find" in resp.json()["detail"].lower()


# ------------------------------------------------------------------ cancel
def test_cancel_missing_job_is_404():
    assert client.post("/api/trending/live/does-not-exist/cancel").status_code == 404


def test_cancel_non_live_job_is_409():
    from clipforge.db import Job, JobStatus, session_factory  # noqa: PLC0415

    session = session_factory()()
    try:
        job = Job(
            source_type="local",
            source_url="clip.mp4",
            title="local",
            status=JobStatus.PENDING.value,
            meta_json={},
        )
        session.add(job)
        session.commit()
        job_id = job.id
    finally:
        session.close()
    try:
        resp = client.post(f"/api/trending/live/{job_id}/cancel")
        assert resp.status_code == 409
        assert "only live capture" in resp.json()["detail"].lower()
    finally:
        session = session_factory()()
        try:
            session.query(Job).filter_by(id=job_id).delete()
            session.commit()
        finally:
            session.close()


def test_cancel_pending_live_job_cancels_it(monkeypatch):
    """A live job whose worker thread has not started (or already
    finished) is cancelled outright by the endpoint."""
    from clipforge.db import Job, JobStatus, session_factory  # noqa: PLC0415

    session = session_factory()()
    try:
        job = Job(
            source_type="live",
            source_url=LIVE_URL_CC,
            title="Live capture — gaming",
            status=JobStatus.PENDING.value,
            meta_json={"live": True, "live_licence": "creativeCommon"},
        )
        session.add(job)
        session.commit()
        job_id = job.id
    finally:
        session.close()
    resp = client.post(f"/api/trending/live/{job_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    session = session_factory()()
    try:
        assert session.get(Job, job_id).status == JobStatus.CANCELLED.value
    finally:
        session.close()


def test_cancel_finished_live_job_is_409():
    from clipforge.db import Job, JobStatus, session_factory  # noqa: PLC0415

    session = session_factory()()
    try:
        job = Job(
            source_type="live",
            source_url=LIVE_URL_CC,
            title="Live capture — gaming",
            status=JobStatus.REVIEW.value,
            meta_json={"live": True, "live_licence": "creativeCommon"},
        )
        session.add(job)
        session.commit()
        job_id = job.id
    finally:
        session.close()
    resp = client.post(f"/api/trending/live/{job_id}/cancel")
    assert resp.status_code == 409
    assert "already" in resp.json()["detail"].lower()
