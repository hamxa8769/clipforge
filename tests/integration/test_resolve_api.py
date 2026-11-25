"""API integration tests — Dashboard one-box (v0.11.0).

  POST /resolve            -> video / channel answers, 422 for bad links
  POST /jobs/clear-finished -> deletes failed / cancelled / blocked jobs
                              (with candidates, stage events, worker
                              runs and referencing posts); active /
                              review / ready jobs are untouched.
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

from clipforge.db import (  # noqa: E402
    ClipCandidate,
    Job,
    JobStageEvent,
    Post,
    WorkerRun,
    get_session,
)
from clipforge.resolve import ResolveError  # noqa: E402

client = TestClient(app)


def _seed_job(status: str, *, with_candidate: bool = False) -> str:
    session = get_session()
    try:
        job = Job(source_type="youtube", source_url="https://www.youtube.com/watch?v=x", status=status)
        session.add(job)
        session.commit()
        job_id = job.id
        if with_candidate:
            cand = ClipCandidate(
                job_id=job_id, n=1, start_s=0.0, end_s=30.0, score=80.0,
                hook_rating=8.0, hook_line="hook", reason="test", status="pending",
            )
            session.add(cand)
            session.commit()
            post = Post(
                clip_candidate_id=cand.id, platform="youtube",
                status="pending_approval",
            )
            session.add(post)
            session.add(JobStageEvent(job_id=job_id, stage="ingest", status="failed", message="boom"))
            session.add(WorkerRun(job_id=job_id, status="failed", error="boom"))
            session.commit()
        return job_id
    finally:
        session.close()


# ------------------------------------------------------------ POST /resolve
def test_resolve_video(monkeypatch) -> None:
    """A video link resolves without creating a job."""
    monkeypatch.setattr(
        "clipforge.resolve.resolve_url",
        lambda url, yt_dlp, **kw: {
            "kind": "video",
            "video": {
                "video_id": "M7lc1UVf-VE", "title": "Fake video",
                "channel_title": "Ch", "channel_id": "UCx",
                "duration_s": 272, "thumbnail_url": None,
                "published_at": "20240115", "view_count": 0,
                "url": "https://www.youtube.com/watch?v=M7lc1UVf-VE",
            },
        },
    )
    res = client.post("/resolve", json={"url": "https://www.youtube.com/watch?v=M7lc1UVf-VE"})
    assert res.status_code == 200
    assert res.json()["kind"] == "video"
    assert res.json()["video"]["video_id"] == "M7lc1UVf-VE"


def test_resolve_channel(monkeypatch) -> None:
    monkeypatch.setattr(
        "clipforge.resolve.resolve_url",
        lambda url, yt_dlp, **kw: {
            "kind": "channel",
            "channel": {"id": "UCx", "title": "Ch", "url": url},
            "videos": [
                {"video_id": "aaa", "title": "First", "channel_title": "Ch",
                 "channel_id": "UCx", "duration_s": 60, "thumbnail_url": None,
                 "published_at": "", "view_count": 0,
                 "url": "https://www.youtube.com/watch?v=aaa"}
            ],
            "source": "ytdlp",
        },
    )
    res = client.post("/resolve", json={"url": "https://www.youtube.com/@Ch"})
    assert res.status_code == 200
    body = res.json()
    assert body["kind"] == "channel"
    assert body["source"] == "ytdlp"
    assert len(body["videos"]) == 1


def test_resolve_unresolvable_link_is_422_with_plain_reason(monkeypatch) -> None:
    def _boom(url, yt_dlp, **kw):
        raise ResolveError(
            "This video is no longer available on YouTube — it was removed.",
            code="youtube_unavailable",
        )

    monkeypatch.setattr("clipforge.resolve.resolve_url", _boom)
    res = client.post("/resolve", json={"url": "https://youtu.be/CMzctRZQlo0"})
    assert res.status_code == 422
    assert "no longer available" in res.text


def test_resolve_non_youtube_is_422(monkeypatch) -> None:
    def _boom(url, yt_dlp, **kw):
        raise ResolveError(
            "That doesn't look like a YouTube video or channel link.",
            code="unknown_url",
        )

    monkeypatch.setattr("clipforge.resolve.resolve_url", _boom)
    res = client.post("/resolve", json={"url": "https://example.com/foo"})
    assert res.status_code == 422


# ------------------------------------------------------------ clear-finished
def test_clear_finished_removes_only_dead_jobs() -> None:
    failed_id = _seed_job("failed", with_candidate=True)
    cancelled_id = _seed_job("cancelled")
    blocked_id = _seed_job("blocked")
    ready_id = _seed_job("ready")
    review_id = _seed_job("review")
    running_id = _seed_job("running")

    res = client.post("/jobs/clear-finished")
    assert res.status_code == 200
    body = res.json()
    # Other test files share this throwaway DB and may have left
    # their own dead jobs behind, so assert "at least" — never an
    # exact count — and check membership, not equality.
    assert body["deleted"] >= 3
    assert {failed_id, cancelled_id, blocked_id} <= set(body["ids"])

    remaining = {j["id"] for j in client.get("/jobs").json()}
    assert {ready_id, review_id, running_id} <= remaining
    assert failed_id not in remaining
    assert cancelled_id not in remaining
    assert blocked_id not in remaining

    # The failed job's candidate, its post, stage event and worker
    # run are gone too — no orphan rows left behind.
    session = get_session()
    try:
        assert session.query(ClipCandidate).filter_by(job_id=failed_id).count() == 0
        assert session.query(JobStageEvent).filter_by(job_id=failed_id).count() == 0
        assert session.query(WorkerRun).filter_by(job_id=failed_id).count() == 0
        assert session.query(Post).filter_by(status="pending_approval").count() == 0
    finally:
        session.close()


def test_clear_finished_is_idempotent() -> None:
    """Clearing twice: the second call finds nothing of its own to
    remove. (Seeded jobs only — other tests' jobs may share the
    throwaway DB, so the first call asserts >= 1, not an exact count.)"""
    failed_id = _seed_job("failed")
    first = client.post("/jobs/clear-finished")
    assert first.status_code == 200
    assert failed_id in first.json()["ids"]
    assert first.json()["deleted"] >= 1
    second = client.post("/jobs/clear-finished")
    assert second.status_code == 200
    assert failed_id not in second.json()["ids"]
