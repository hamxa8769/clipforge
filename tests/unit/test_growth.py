"""Unit tests for the Growth view (v0.6.0, Worker C).

Pure-function tests: views/day, engagement, trend, advice rules, and
the YouTube stats fetcher (mocked transport — no network, no key).
The HTTP endpoints are covered in tests/integration/test_growth_api.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from clipforge_api.growth import (
    GENERAL_BEST_PRACTICE,
    GrowthFetchError,
    compute_advice,
    engagement_rate,
    enrich_clips,
    fetch_youtube_stats,
    trend_for,
    views_per_day,
)

NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)


def _row(post_id: str, *, views, vpd, hour=18, niche="gaming", title="Clip"):
    return {
        "post_id": post_id,
        "posted_video_id": f"vid-{post_id}",
        "platform": "youtube",
        "title": title,
        "posted_at": datetime(2026, 11, 20, hour, 0, 0, tzinfo=UTC).isoformat(),
        "niche": niche,
        "views": views,
        "likes": 10,
        "comments": 2,
        "views_per_day": vpd,
        "engagement_pct": 1.0,
        "trend": "new",
        "stale": False,
        "stats_status": "live",
    }


# ------------------------------------------------------------- views per day
def test_views_per_day_divides_by_days_since_posting():
    posted = NOW - timedelta(days=4)
    assert views_per_day(1000, posted, NOW) == 250.0


def test_views_per_day_minimum_window_is_one_hour():
    # Posted 10 minutes ago: must not explode (1000 views / 10 min).
    posted = NOW - timedelta(minutes=10)
    assert views_per_day(1000, posted, NOW) == 24000.0  # 1000 per (1/24 day)


def test_views_per_day_zero_views_is_zero():
    assert views_per_day(0, NOW - timedelta(days=2), NOW) == 0.0


# -------------------------------------------------------------- engagement
def test_engagement_rate_is_likes_over_views_pct():
    assert engagement_rate(50, 1000) == 5.0


def test_engagement_rate_none_when_no_views():
    # 0% would pretend the clip was measured and failed.
    assert engagement_rate(0, 0) is None


# ------------------------------------------------------------------- trend
def test_trend_new_without_previous_snapshot():
    assert trend_for(100, None, NOW) == "new"


def test_trend_growing_when_views_rose():
    prev = {"views": 50, "fetched_at": (NOW - timedelta(days=1)).isoformat()}
    assert trend_for(120, prev, NOW) == "growing"


def test_trend_flat_when_views_unchanged():
    prev = {"views": 120, "fetched_at": (NOW - timedelta(days=1)).isoformat()}
    assert trend_for(120, prev, NOW) == "flat"


def test_trend_falling_when_views_dropped():
    prev = {"views": 200, "fetched_at": (NOW - timedelta(days=1)).isoformat()}
    assert trend_for(150, prev, NOW) == "falling"


def test_trend_unknown_when_previous_snapshot_too_fresh():
    prev = {"views": 50, "fetched_at": (NOW - timedelta(minutes=10)).isoformat()}
    assert trend_for(60, prev, NOW) == "unknown"


# ------------------------------------------------------------------ advice
def test_advice_below_threshold_is_general_only():
    rows = [_row("a", views=100, vpd=50.0), _row("b", views=200, vpd=100.0)]
    advice = compute_advice(rows, NOW)
    # Recommendations (best hour, niche ranking) are general-only below
    # the threshold — no pattern is claimed from 2 clips.
    assert advice["best_posting_times"]["basis"]["kind"] == "general"
    assert advice["niches"]["basis"]["kind"] == "general"
    assert advice["niches"]["ranking"] == []
    assert "not your data yet" in advice["best_posting_times"]["basis"]["label"]
    # ...but the top-titles list is a factual listing of their own clips,
    # so it honestly says "from your 2 posted clips".
    assert advice["best_titles"]["basis"]["kind"] == "own_data"
    assert advice["best_titles"]["basis"]["n"] == 2
    assert [t["post_id"] for t in advice["best_titles"]["top"]] == ["b", "a"]
    # General guidance is still present and labelled as general.
    assert advice["general_best_practice"] == GENERAL_BEST_PRACTICE
    assert len(GENERAL_BEST_PRACTICE) >= 3


def test_advice_with_enough_clips_uses_own_data():
    rows = [
        _row(f"c{i}", views=100 * (i + 1), vpd=10.0 * (i + 1), hour=18 + (i % 3)) for i in range(6)
    ]
    advice = compute_advice(rows, NOW)
    assert advice["best_posting_times"]["basis"]["kind"] == "own_data"
    assert advice["best_posting_times"]["basis"]["n"] == 6
    # Best hour is the one with the highest mean views/day.
    assert advice["best_posting_times"]["best_hours_utc"][0] in (18, 19, 20)
    # Top title is the highest views/day clip.
    assert advice["best_titles"]["top"][0]["post_id"] == "c5"
    # Niches ranked by mean views/day.
    assert advice["niches"]["ranking"][0]["niche"] == "gaming"
    assert advice["niches"]["ranking"][0]["clips"] == 6
    # "What to post next" is built from own data.
    assert advice["what_to_post_next"][0]["basis"].startswith("from your 6 posted clips")


def test_advice_ignores_clips_without_stats():
    rows = [_row("a", views=None, vpd=None), _row("b", views=500, vpd=250.0)]
    advice = compute_advice(rows, NOW)
    # Only 1 measured clip -> recommendations are general, even though
    # 2 rows exist; the one measured clip is still listed factually.
    assert advice["best_posting_times"]["basis"]["kind"] == "general"
    assert advice["best_titles"]["basis"]["kind"] == "own_data"
    assert advice["best_titles"]["basis"]["n"] == 1


def test_advice_skips_empty_niche_instead_of_inventing_one():
    rows = [_row(f"c{i}", views=100, vpd=10.0, hour=18 + (i % 2), niche="") for i in range(6)]
    advice = compute_advice(rows, NOW)
    assert advice["niches"]["ranking"] == []


def test_advice_best_hour_needs_two_distinct_hours():
    rows = [_row(f"c{i}", views=100, vpd=10.0, hour=18) for i in range(6)]
    advice = compute_advice(rows, NOW)
    # All clips at one hour: no honest "best hour" from own data.
    assert advice["best_posting_times"]["hours"] == []
    assert advice["best_posting_times"]["basis"]["kind"] == "general"


def test_advice_empty_state_is_honest():
    advice = compute_advice([], NOW)
    assert advice["best_titles"]["top"] == []
    assert advice["niches"]["ranking"] == []
    assert "Post your first clips" in advice["what_to_post_next"][0]["text"]


# --------------------------------------------------------------- enrichment
def test_enrich_clips_prefers_live_stats_and_marks_stale_snapshot():
    clips = [
        {
            "post_id": "p1",
            "posted_video_id": "v1",
            "platform": "youtube",
            "title": "Live one",
            "posted_at": (NOW - timedelta(days=2)).isoformat(),
            "niche": "gaming",
        },
        {
            "post_id": "p2",
            "posted_video_id": "v2",
            "platform": "youtube",
            "title": "Stale one",
            "posted_at": (NOW - timedelta(days=2)).isoformat(),
            "niche": "tech",
        },
        {
            "post_id": "p3",
            "posted_video_id": None,
            "platform": "youtube",
            "title": "No id",
            "posted_at": (NOW - timedelta(days=2)).isoformat(),
            "niche": "",
        },
        {
            "post_id": "p4",
            "posted_video_id": "v4",
            "platform": "tiktok",
            "title": "TikTok",
            "posted_at": (NOW - timedelta(days=2)).isoformat(),
            "niche": "",
        },
    ]
    stats = {"v1": {"views": 1000, "likes": 50, "comments": 5}}
    snapshots = {
        "v2": {
            "views": 400,
            "likes": 20,
            "comments": 1,
            "fetched_at": (NOW - timedelta(days=1)).isoformat(),
        }
    }
    rows = enrich_clips(clips, stats, snapshots, NOW)
    live, stale, no_id, tiktok = rows
    assert live["views"] == 1000 and live["views_per_day"] == 500.0
    assert live["stats_status"] == "live" and live["stale"] is False
    assert stale["views"] == 400 and stale["stale"] is True
    assert stale["stats_status"] == "stale"
    assert no_id["views"] is None and no_id["stats_status"] == "no_video_id"
    assert tiktok["views"] is None and tiktok["stats_status"] == "unsupported"


# ------------------------------------------------------- YouTube stats fetch
def _mock_client(payload: dict, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "videos" in request.url.path
        assert request.url.params["part"] == "statistics"
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_youtube_stats_parses_counts():
    client = _mock_client(
        {
            "items": [
                {
                    "id": "abc",
                    "statistics": {
                        "viewCount": "1234",
                        "likeCount": "56",
                        "commentCount": "7",
                    },
                },
                {"id": "gone", "statistics": {}},
            ]
        }
    )
    out = fetch_youtube_stats(["abc", "gone"], api_key="test-key", client=client)
    assert out["abc"] == {"views": 1234, "likes": 56, "comments": 7}
    # An item with empty statistics parses to zeros, not an error.
    assert out["gone"] == {"views": 0, "likes": 0, "comments": 0}


def test_fetch_youtube_stats_empty_ids_no_request():
    out = fetch_youtube_stats([], api_key="test-key")
    assert out == {}


def test_fetch_youtube_stats_needs_auth():
    with pytest.raises(GrowthFetchError):
        fetch_youtube_stats(["abc"])


def test_fetch_youtube_stats_http_error_raises():
    client = _mock_client({"error": {"message": "quota"}}, status=403)
    with pytest.raises(GrowthFetchError, match="HTTP 403"):
        fetch_youtube_stats(["abc"], api_key="test-key", client=client)


def test_fetch_youtube_stats_missing_ids_are_absent_not_zero():
    # The API only returns ids it knows: a deleted/private video is
    # absent from the result — the caller must not invent 0 views for it.
    client = _mock_client({"items": [{"id": "abc", "statistics": {"viewCount": "10"}}]})
    out = fetch_youtube_stats(["abc", "deleted-id"], api_key="k", client=client)
    assert "deleted-id" not in out
