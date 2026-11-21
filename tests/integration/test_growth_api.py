"""Growth API integration tests (v0.6.0, Worker C).

The growth router is mounted on a fresh FastAPI app here — exactly the
one-line wiring ``clipforge_api/app.py`` needs
(``app.include_router(growth_router)``) — so these tests also prove the
router is self-contained. Seeded posts are real ``published`` Post rows;
the YouTube fetch is stubbed at ``fetch_all_stats`` (the HTTP layer is
covered by unit tests in tests/unit/test_growth.py). No network, no keys.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import clipforge_api.growth as growth
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clipforge.db import ClipCandidate, Job, Post, get_session

NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)

# Ids seeded by this file, deleted after each test: the suite shares one
# throwaway DB per process, so one test's published posts must never leak
# into the next test's "posted clips" (or into other files' tests).
_seeded_post_ids: list[str] = []


@pytest.fixture(autouse=True)
def _clean_seeded():
    yield
    if not _seeded_post_ids:
        return
    session = get_session()
    try:
        posts = session.query(Post).filter(Post.id.in_(_seeded_post_ids)).all()
        cand_ids = [p.clip_candidate_id for p in posts if p.clip_candidate_id]
        job_ids = [
            c.job_id
            for c in session.query(ClipCandidate).filter(ClipCandidate.id.in_(cand_ids)).all()
        ]
        session.query(Post).filter(Post.id.in_(_seeded_post_ids)).delete(synchronize_session=False)
        if cand_ids:
            session.query(ClipCandidate).filter(ClipCandidate.id.in_(cand_ids)).delete(
                synchronize_session=False
            )
        if job_ids:
            session.query(Job).filter(Job.id.in_(job_ids)).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()
        _seeded_post_ids.clear()


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """Fresh app + temp data dir (snapshots land in tmp_path, not ./data)."""
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path))
    application = FastAPI()
    application.include_router(growth.router)
    return application


@pytest.fixture()
def client(app):
    return TestClient(app)


def _seed_posted_clip(
    *,
    video_id: str | None = "vid-1",
    platform: str = "youtube",
    title: str = "Test clip",
    niche: str = "gaming",
    posted_days_ago: int = 2,
    posted_hour: int = 18,
) -> str:
    """A published post with a provider video id, linked to an Agent-style
    job carrying the niche in meta_json — the posted-clip contract."""
    session = get_session()
    try:
        posted_at = (NOW - timedelta(days=posted_days_ago)).replace(
            hour=posted_hour, minute=0, second=0, microsecond=0
        )
        job = Job(
            source_type="youtube",
            source_url="https://www.youtube.com/watch?v=source",
            title="Agent run — Gaming",
            own_content=True,
            status="ready",
            meta_json={"agent": True, "niche": niche},
        )
        session.add(job)
        session.commit()
        cand = ClipCandidate(
            job_id=job.id,
            n=1,
            start_s=0.0,
            end_s=30.0,
            score=85.0,
            hook_rating=8.0,
            hook_line="Stop doing this",
            reason="seeded",
            status="approved",
        )
        session.add(cand)
        session.commit()
        post = Post(
            clip_candidate_id=cand.id,
            platform=platform,
            status="published",
            seo_json={"title": title},
            publish_result_json={
                "platform": platform,
                "status": "published",
                "provider_post_id": video_id,
            },
            published_at=posted_at,
        )
        session.add(post)
        session.commit()
        _seeded_post_ids.append(post.id)
        return post.id
    finally:
        session.close()


def _stats_for(video_ids_stats: dict[str, dict[str, int]]):
    def fake_fetch(clips):
        stats = {
            vid: values
            for vid, values in video_ids_stats.items()
            if any(c.get("posted_video_id") == vid for c in clips)
        }
        return stats, {"youtube": "live"}

    return fake_fetch


# ------------------------------------------------------------------ empty
def test_growth_empty_state_is_honest(client):
    resp = client.get("/growth")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_posted_clips"] is False
    assert body["posted_clips"] == []
    assert body["totals"]["posted_clips"] == 0
    assert body["totals"]["total_views"] == 0
    # Advice exists but is labelled general — never invented own-data.
    assert body["advice"]["best_titles"]["basis"]["kind"] == "general"
    assert body["advice"]["best_titles"]["top"] == []
    assert "Post your first clips" in body["advice"]["what_to_post_next"][0]["text"]


# -------------------------------------------------------------- populated
def test_growth_populated_from_published_posts(client, monkeypatch):
    for i in range(6):
        _seed_posted_clip(
            video_id=f"vid-{i}",
            title=f"Gaming clip {i}",
            posted_hour=18 + (i % 3),
        )
    stats = {
        f"vid-{i}": {"views": 1000 * (i + 1), "likes": 50 * (i + 1), "comments": i}
        for i in range(6)
    }
    monkeypatch.setattr(growth, "fetch_all_stats", _stats_for(stats))

    resp = client.get("/growth")
    assert resp.status_code == 200
    body = resp.json()
    assert body["totals"]["posted_clips"] == 6
    assert body["totals"]["clips_with_stats"] == 6
    assert body["totals"]["total_views"] == 21000
    rows = {r["posted_video_id"]: r for r in body["posted_clips"]}
    # 6000 views over ~2 days -> ~3000 views/day (exact value depends on
    # the real clock at test time, so check the range, not the digit).
    assert 2800.0 < rows["vid-5"]["views_per_day"] < 3300.0
    assert rows["vid-5"]["engagement_pct"] == 5.0
    assert rows["vid-5"]["stats_status"] == "live"
    # No previous snapshot yet -> trend is "new", not invented.
    assert rows["vid-5"]["trend"] == "new"
    advice = body["advice"]
    assert advice["best_posting_times"]["basis"]["kind"] == "own_data"
    assert advice["best_titles"]["top"][0]["title"] == "Gaming clip 5"
    assert advice["niches"]["ranking"][0]["niche"] == "gaming"


def test_growth_trend_growing_against_old_snapshot(client, tmp_path, monkeypatch):
    _seed_posted_clip(video_id="vid-1", title="Gaming clip")
    # Yesterday's snapshot: 500 views, fetched 25h ago.
    snap_path = tmp_path / "growth_snapshots.json"
    snap_path.write_text(
        json.dumps(
            {
                "vid-1": {
                    "views": 500,
                    "likes": 25,
                    "comments": 1,
                    "fetched_at": (NOW - timedelta(hours=25)).isoformat(),
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        growth, "fetch_all_stats", _stats_for({"vid-1": {"views": 900, "likes": 45, "comments": 3}})
    )
    resp = client.get("/growth")
    assert resp.status_code == 200
    row = resp.json()["posted_clips"][0]
    assert row["views"] == 900
    assert row["trend"] == "growing"


def test_growth_refresh_stores_snapshot(client, tmp_path, monkeypatch):
    _seed_posted_clip(video_id="vid-1", title="Gaming clip")
    monkeypatch.setattr(
        growth, "fetch_all_stats", _stats_for({"vid-1": {"views": 700, "likes": 10, "comments": 0}})
    )
    resp = client.post("/growth/refresh")
    assert resp.status_code == 200
    assert resp.json()["posted_clips"][0]["views"] == 700
    saved = json.loads((tmp_path / "growth_snapshots.json").read_text(encoding="utf-8"))
    assert saved["vid-1"]["views"] == 700
    assert "fetched_at" in saved["vid-1"]


def test_growth_no_credentials_means_no_fake_stats(client, monkeypatch):
    """No YouTube token and no YOUTUBE_API_KEY in this test env: the clip
    is listed with null stats and status "not_connected" — never zeros."""
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    _seed_posted_clip(video_id="vid-1", title="Gaming clip")
    resp = client.get("/growth")
    assert resp.status_code == 200
    row = resp.json()["posted_clips"][0]
    assert row["views"] is None
    assert row["views_per_day"] is None
    assert resp.json()["platform_status"]["youtube"] == "not_connected"


def test_growth_unsupported_platform_is_labelled(client, monkeypatch):
    _seed_posted_clip(video_id="tt-1", platform="tiktok", title="TikTok clip")
    monkeypatch.setattr(growth, "fetch_all_stats", lambda clips: ({}, {"tiktok": "unsupported"}))
    resp = client.get("/growth")
    assert resp.status_code == 200
    row = resp.json()["posted_clips"][0]
    assert row["stats_status"] == "unsupported"
    assert row["views"] is None


def test_growth_draft_post_is_not_a_posted_clip(client):
    session = get_session()
    try:
        post = Post(
            platform="youtube",
            status="pending_approval",
            seo_json={"title": "Not posted yet"},
            publish_result_json={"provider_post_id": "vid-x"},
        )
        session.add(post)
        session.commit()
        _seeded_post_ids.append(post.id)
    finally:
        session.close()
    resp = client.get("/growth")
    assert resp.json()["has_posted_clips"] is False


def test_growth_advice_uses_snapshots_without_live_fetch(client, tmp_path, monkeypatch):
    """GET /growth/advice never calls the network: it reads snapshots."""
    _seed_posted_clip(video_id="vid-1", title="Gaming clip")
    (tmp_path / "growth_snapshots.json").write_text(
        json.dumps(
            {
                "vid-1": {
                    "views": 800,
                    "likes": 40,
                    "comments": 2,
                    "fetched_at": (NOW - timedelta(days=1)).isoformat(),
                }
            }
        ),
        encoding="utf-8",
    )

    def _boom(clips):
        raise AssertionError("advice must not fetch live stats")

    monkeypatch.setattr(growth, "fetch_all_stats", _boom)
    resp = client.get("/growth/advice")
    assert resp.status_code == 200
    # 1 measured clip < 5 -> recommendations are general, honestly labelled.
    assert resp.json()["best_posting_times"]["basis"]["kind"] == "general"
    assert resp.json()["niches"]["ranking"] == []
