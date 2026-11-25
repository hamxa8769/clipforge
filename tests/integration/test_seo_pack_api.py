"""API integration tests — SEO pack + Channel SEO (v0.11.0, Worker B).

Real endpoints (docs/api.md):
  GET  /clips/{candidate_id}/seo-pack -> 200 {platforms: {youtube: {...}}}
  POST /seo-pack/preview              -> 200 {platforms: {...}}
  GET  /channel-seo                  -> 400 without a link or a connected
                                         channel; 200 with a mocked live read
  POST /channel-seo/analyze          -> 400 on a video link; 200 on a mocked
                                         channel read
  GET  /channel-seo/connected        -> 404 when no channel is connected

The live channel read is mocked (monkeypatched ``channel_seo.fetch_channel`` /
``analyze_live``) — no network, no yt-dlp. Candidates are seeded directly
in the test DB, same as test_api_smoke.py.
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

from clipforge.db import ClipCandidate, Job, get_session  # noqa: E402

client = TestClient(app)


def _seed_clip() -> str:
    session = get_session()
    try:
        job = Job(
            title="Morning routine: start strong",
            source_type="youtube",
            source_url="https://www.youtube.com/watch?v=example",
            status="review",
        )
        session.add(job)
        session.commit()
        cand = ClipCandidate(
            job_id=job.id,
            n=1,
            start_s=10.0,
            end_s=40.0,
            score=82.0,
            hook_rating=8.0,
            hook_line="Stop doing this first thing in the morning",
            reason="test candidate",
            transcript_excerpt="First I drink water, then I stretch for five minutes.",
            source_title="Morning routine: start strong",
            source_channel="Daily Clips",
            status="pending",
        )
        session.add(cand)
        session.commit()
        return cand.id
    finally:
        session.close()


def test_seo_pack_preview():
    res = client.post(
        "/seo-pack/preview",
        json={"keyword": "morning routine", "hook": "Stop doing this first"},
    )
    assert res.status_code == 200, res.text
    pack = res.json()
    assert set(pack["platforms"]) == {"youtube", "tiktok", "instagram", "facebook", "x"}
    yt = pack["platforms"]["youtube"]
    assert yt["title"]
    assert 2 <= len(yt["title_options"]) <= 3
    assert "morning routine" in yt["description"][:200].lower()
    assert yt["tags"]
    assert yt["hashtags"]
    assert "YouTube Shorts" in pack["copy_all"]


def test_seo_pack_preview_keyword_required():
    res = client.post("/seo-pack/preview", json={"keyword": " ", "hook": "x"})
    assert res.status_code in (400, 422)


def test_clip_seo_pack():
    cand_id = _seed_clip()
    res = client.get(f"/clips/{cand_id}/seo-pack")
    assert res.status_code == 200, res.text
    pack = res.json()
    assert pack["clip_candidate_id"] == cand_id
    assert pack["keyword"] == "Morning routine"
    # The transcript excerpt lands in the description.
    assert "What was said" in pack["platforms"]["youtube"]["description"]
    # The channel name lands in the search tags ("clips" is filtered as
    # a noise word, so "Daily Clips" contributes "daily").
    assert "daily" in pack["platforms"]["youtube"]["tags"]


def test_clip_seo_pack_unknown_candidate():
    res = client.get("/clips/does-not-exist/seo-pack")
    assert res.status_code == 404


def test_channel_seo_needs_link_or_connected_channel():
    res = client.get("/channel-seo")
    # No link pasted and no channel connected in the test env.
    assert res.status_code == 400
    assert "channel link" in res.json()["detail"].lower()


def test_channel_seo_connected_404_without_channel():
    res = client.get("/channel-seo/connected")
    assert res.status_code == 404


def test_channel_seo_analyze_rejects_video_link():
    res = client.post(
        "/channel-seo/analyze",
        json={"channel_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    assert res.status_code == 400
    assert "video link" in res.json()["detail"]


def test_channel_seo_analyze_live_mocked(monkeypatch):
    import clipforge_api.seo_pack as seo_api

    from clipforge import channel_seo

    def fake_live(
        channel_url, topic="", limit=30, data_dir=None, youtube_api_key=None, use_cache=True
    ):
        data = {
            "channel_id": "UC123",
            "channel_name": "Daily Clips",
            "channel_url": channel_url,
            "subscriber_count": 12000,
            "videos": [
                {
                    "video_id": "a",
                    "title": "How to start your morning routine",
                    "url": "https://www.youtube.com/watch?v=a",
                    "views": 50000,
                    "duration_s": 55,
                    "published_at": "2026-11-01",
                },
                {
                    "video_id": "b",
                    "title": "Morning routine mistakes",
                    "url": "https://www.youtube.com/watch?v=b",
                    "views": 120000,
                    "duration_s": 48,
                    "published_at": "2026-10-28",
                },
            ],
        }
        analysis = channel_seo.analyze_channel(data, topic=topic)
        analysis["cached"] = False
        analysis["fetched_at"] = "2026-11-25T00:00:00+00:00"
        analysis["api_enrichment"] = {"used": False, "note": "No key."}
        return analysis

    monkeypatch.setattr(seo_api.channel_seo, "analyze_live", fake_live)
    res = client.post(
        "/channel-seo/analyze",
        json={"channel_url": "https://www.youtube.com/@dailyclips", "topic": "evening routine"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["channel_name"] == "Daily Clips"
    assert body["top_videos"][0]["video_id"] == "b"  # most views first
    assert body["suggested_titles"]
    assert "evening" in body["suggested_tags"]
    assert body["api_enrichment"]["used"] is False


def test_channel_seo_get_uses_cache(monkeypatch, tmp_path):
    import clipforge_api.seo_pack as seo_api

    from clipforge import channel_seo

    cached = {
        "channel_id": "UCabcdefghijklmnopqrstuv",
        "channel_name": "Cached Channel",
        "channel_url": "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
        "subscriber_count": 5,
        "videos_fetched": 2,
        "top_videos": [],
        "what_works": {"common_words": [], "title_patterns": {}, "basis": "cached"},
        "suggested_titles": [],
        "suggested_tags": [],
        "cached": False,
        "fetched_at": "2026-11-25T00:00:00+00:00",
    }
    channel_seo.save_cached(tmp_path, "UCabcdefghijklmnopqrstuv", cached)
    monkeypatch.setattr(seo_api, "_data_dir", lambda: tmp_path)

    def _boom(*args, **kwargs):
        raise AssertionError("live read must not run when the cache is fresh")

    monkeypatch.setattr(seo_api.channel_seo, "analyze_live", _boom)
    res = client.get(
        "/channel-seo",
        params={"channel_url": "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["channel_name"] == "Cached Channel"
    assert res.json()["cached"] is True
