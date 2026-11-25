"""API integration tests — Trending router (v0.6.0, Worker B).

Contract (exact paths, router mounted standalone here — app.py
registration is Worker A's; the router is mounted on a fresh app so
these tests verify the router itself):

  GET  /api/trending/niches
  GET  /api/trending/credentials
  POST /api/trending/api-key {api_key}
  GET  /api/trending/search?niche=&max_results=
  POST /api/trending/match {channel_url, trending_title}
  POST /api/trending/run {…}
  GET  /api/trending/status
  GET  /api/trending/jobs/{job_id}

YouTube HTTP is mocked by monkeypatching ``httpx.Client`` in the
router module — no test ever hits the real YouTube API. Every error
asserted here is plain language: no traceback, no secret value in any
response. The full loop (run -> job -> posts) uses the agent demo
transcript path (no download, no FFmpeg).
"""

from __future__ import annotations

import time

import httpx
import pytest
from clipforge_api import trending as trending_router  # noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.test_agent_niches import GAMING_TEXT  # noqa: E402

app = FastAPI()
app.include_router(trending_router.router)
client = TestClient(app)

OWN_VIDEO = "https://www.youtube.com/watch?v=AAAAAAAAAAA"
TRENDING_VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
TRENDING_ID = "dQw4w9WgXcQ"
TRENDING_TITLE = "Best gaming clutch moments you have to see"
DEMO_TRANSCRIPT = (GAMING_TEXT + " ") * 3


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Each test gets its own data dir (agent.json + secrets live there)."""
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _clean_agent_rows():
    """Remove exactly the rows this agent created (jobs flagged agent
    in meta), so later tests see the DB as they left it."""
    from clipforge.db import ClipCandidate, Job, Post, session_factory  # noqa: PLC0415

    yield
    session = session_factory()()
    try:
        agent_jobs = [j for j in session.query(Job).all() if (j.meta_json or {}).get("agent")]
        ids = {j.id for j in agent_jobs}
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
        session.query(Job).filter(Job.id.in_(ids)).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def _fake_youtube_client():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": {"videoId": TRENDING_ID},
                            "snippet": {"title": TRENDING_TITLE, "channelTitle": "Trend Chan"},
                        }
                    ]
                },
            )
        if request.url.path.endswith("/videos"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": TRENDING_ID,
                            "snippet": {
                                "title": TRENDING_TITLE,
                                "channelTitle": "Trend Chan",
                                "channelId": "UCdQw4w9WgXcQ",
                                "publishedAt": "2026-11-21T10:00:00Z",
                            },
                            "statistics": {
                                "viewCount": "120000",
                                "likeCount": "9000",
                                "commentCount": "800",
                            },
                        }
                    ]
                },
            )
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ niches / credentials
def test_niches_lists_presets():
    resp = client.get("/api/trending/niches")
    assert resp.status_code == 200
    body = resp.json()
    assert "gaming" in body["niches"]
    assert body["labels"]["gaming"] == "Gaming"
    assert "notice" not in body  # v0.11.0: notice removed


def test_credentials_not_usable_without_connection_or_key():
    resp = client.get("/api/trending/credentials")
    assert resp.status_code == 200
    body = resp.json()
    assert body["usable"] is False
    assert body["oauth"]["connected"] is False
    assert body["api_key"]["configured"] is False
    assert "connect your youtube channel" in body["message"].lower()


def test_save_api_key_returns_masked_view_only():
    resp = client.post("/api/trending/api-key", json={"api_key": "AIzaFAKEKEY1234"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["last4"] == "1234"
    assert "AIzaFAKEKEY1234" not in resp.text
    creds = client.get("/api/trending/credentials").json()
    assert creds["api_key"]["configured"] is True
    assert creds["usable"] is True
    assert creds["api_key"]["source"] == "stored"


def test_clear_api_key():
    client.post("/api/trending/api-key", json={"api_key": "AIzaFAKEKEY1234"})
    resp = client.post("/api/trending/api-key", json={"api_key": ""})
    assert resp.json()["configured"] is False


# ------------------------------------------------------------------ search
def test_search_without_credentials_is_plain_422():
    resp = client.get("/api/trending/search", params={"niche": "gaming"})
    assert resp.status_code == 422
    assert "connect your youtube channel" in resp.json()["detail"].lower()
    assert "invented" in resp.json()["detail"].lower()


def test_search_with_saved_key_returns_live_shaped_results(monkeypatch):
    client.post("/api/trending/api-key", json={"api_key": "AIzaFAKEKEY1234"})
    fake = _fake_youtube_client()
    monkeypatch.setattr(trending_router.httpx, "Client", lambda **kwargs: fake)
    resp = client.get("/api/trending/search", params={"niche": "gaming"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "api_key"
    assert body["count"] == 1
    result = body["results"][0]
    assert result["video_id"] == TRENDING_ID
    assert result["title"] == TRENDING_TITLE
    assert result["channel_title"] == "Trend Chan"
    assert result["view_count"] == 120_000
    assert result["like_count"] == 9_000
    assert result["trend_score"] > 0
    assert result["url"] == TRENDING_VIDEO
    assert "notice" not in body  # v0.11.0: notice removed


# ------------------------------------------------------------------ match
def test_match_without_yt_dlp_is_plain_422(monkeypatch):
    # yt-dlp resolution now goes through resolve_yt_dlp_path (bundled
    # install first, then PATH) — patch that seam, not shutil.which.
    monkeypatch.setattr("clipforge.trending.resolve_yt_dlp_path", lambda *a, **k: None)
    resp = client.post(
        "/api/trending/match",
        json={
            "channel_url": "https://www.youtube.com/@somechannel",
            "trending_title": TRENDING_TITLE,
        },
    )
    assert resp.status_code == 422
    assert "yt-dlp" in resp.json()["detail"]


# ------------------------------------------------------------------ run: guardrail
def test_run_accepts_trending_video_as_source():
    """v0.11.0: the trending video itself is accepted as a clip
    source — the old guardrail is gone."""
    resp = client.post(
        "/api/trending/run",
        json={
            "niche": "gaming",
            "trending_video_id": TRENDING_ID,
            "trending_title": TRENDING_TITLE,
            "own_source_url": TRENDING_VIDEO,
            "own_source_type": "video",
            "rights_confirmed": True,
            "demo_transcript_text": DEMO_TRANSCRIPT,
        },
    )
    assert resp.status_code == 202, resp.text
    state = _wait_for_done()
    assert state["state"] == "done", state.get("last_error")


def test_run_needs_no_rights_confirmation():
    """v0.11.0: the rights gate is gone — a run starts with the
    (no longer asked-for) confirmation unset."""
    resp = client.post(
        "/api/trending/run",
        json={
            "niche": "gaming",
            "trending_video_id": TRENDING_ID,
            "trending_title": TRENDING_TITLE,
            "own_source_url": OWN_VIDEO,
            "own_source_type": "video",
            "rights_confirmed": False,
            "demo_transcript_text": DEMO_TRANSCRIPT,
        },
    )
    assert resp.status_code == 202, resp.text
    state = _wait_for_done()
    assert state["state"] == "done", state.get("last_error")


# ------------------------------------------------------------------ run: full loop (demo path)
def _wait_for_done(timeout_s: float = 30.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        state = client.get("/api/trending/status").json()
        if state["state"] in {"done", "error"}:
            return state
        time.sleep(0.5)
    raise AssertionError(f"trending run did not finish in {timeout_s}s")


def test_run_full_loop_demo_produces_trending_job_with_posts():
    resp = client.post(
        "/api/trending/run",
        json={
            "niche": "gaming",
            "trending_video_id": TRENDING_ID,
            "trending_title": TRENDING_TITLE,
            "own_source_url": OWN_VIDEO,
            "own_source_type": "video",
            "rights_confirmed": True,
            "top_n": 1,
            "platforms": ["youtube"],
            "demo_transcript_text": DEMO_TRANSCRIPT,
        },
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["ok"] is True
    assert body["own_source_url"] == OWN_VIDEO

    state = _wait_for_done()
    assert state["state"] == "done", state.get("last_error")
    assert state["trending_video_id"] == TRENDING_ID
    assert state["trending_title"] == TRENDING_TITLE
    assert state["queued"] >= 1
    job_id = state["current_job"]
    assert job_id

    job = client.get(f"/api/trending/jobs/{job_id}")
    assert job.status_code == 200
    record = job.json()
    assert record["trending_video_id"] == TRENDING_ID
    assert record["trending_title"] == TRENDING_TITLE
    assert record["trending_url"] == TRENDING_VIDEO
    assert record["source"] == OWN_VIDEO
    assert record["demo"] is True
    assert len(record["posts"]) >= 1
    post = record["posts"][0]
    assert post["platform"] == "youtube"
    assert post["status"] == "pending_approval"  # approval-first
    assert post["provider_post_id"] is None  # not posted yet
    assert post["title"]  # SEO was generated, tuned to the trend


def test_trending_job_404():
    assert client.get("/api/trending/jobs/does-not-exist").status_code == 404
