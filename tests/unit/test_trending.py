"""Unit tests — live niche trending search + topic matching (v0.6.0).

No test hits the real YouTube API: ``search_trending`` is exercised
with a fake httpx client, and the trend-score formula is tested
directly. The guardrail (a trending video URL is never a clip source)
is asserted here and again at the API level.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from clipforge.trending import (
    TRENDING_NOTICE,
    TrendingCredentials,
    TrendingError,
    TrendingVideo,
    extract_youtube_video_id,
    is_trending_source,
    niche_search_query,
    refuse_trending_source,
    search_trending,
    topic_match_score,
    trend_score,
)

NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)


# ------------------------------------------------------------------ trend score
def test_trend_score_new_fast_video_beats_old_slow_video():
    new = trend_score(100_000, 5_000, 500, NOW - timedelta(hours=2), now=NOW)
    old = trend_score(100_000, 5_000, 500, NOW - timedelta(days=6), now=NOW)
    assert new > old
    assert 0.0 <= new <= 100.0


def test_trend_score_engagement_breaks_velocity_tie():
    low_eng = trend_score(50_000, 100, 10, NOW - timedelta(hours=10), now=NOW)
    high_eng = trend_score(50_000, 4_000, 400, NOW - timedelta(hours=10), now=NOW)
    assert high_eng > low_eng


def test_trend_score_zero_views_is_zero():
    assert trend_score(0, 0, 0, NOW - timedelta(hours=1), now=NOW) == 0.0


def test_trend_score_saturates_at_100():
    assert trend_score(10_000_000, 900_000, 100_000, NOW - timedelta(hours=1), now=NOW) == 100.0


# ------------------------------------------------------------------ video ids / guardrail
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/@somechannel", None),
        ("not a url", None),
        (None, None),
    ],
)
def test_extract_youtube_video_id(url, expected):
    assert extract_youtube_video_id(url) == expected


def test_is_trending_source_same_video_id():
    assert is_trending_source("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ")
    assert is_trending_source("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ")


def test_is_trending_source_different_video_is_not_trending_source():
    # The user's OWN video on the same topic is a different video id —
    # that is the only accepted ingest input.
    assert not is_trending_source("https://www.youtube.com/watch?v=AAAAAAAAAAA", "dQw4w9WgXcQ")
    assert not is_trending_source("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "")


def test_refuse_trending_source_raises_plain_message():
    with pytest.raises(TrendingError, match="cannot clip another creator"):
        refuse_trending_source("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ")
    # A different (own) video passes silently.
    refuse_trending_source("https://www.youtube.com/watch?v=AAAAAAAAAAA", "dQw4w9WgXcQ")


# ------------------------------------------------------------------ niche query
def test_niche_search_query_preset_uses_label_and_keywords():
    query = niche_search_query("gaming")
    assert "Gaming" in query
    assert "game" in query.lower()


def test_niche_search_query_custom_niche_uses_own_words():
    assert niche_search_query("street food") == "Street Food street food"


# ------------------------------------------------------------------ topic match
def test_topic_match_score_same_topic_high():
    assert (
        topic_match_score("Best gaming clutch moments 2026", "My best gaming clutch moment") > 0.5
    )


def test_topic_match_score_unrelated_low():
    assert topic_match_score("Best gaming clutch moments", "How to bake sourdough bread") == 0.0


def test_topic_match_score_empty_trending_is_zero():
    assert topic_match_score("", "anything at all") == 0.0


# ------------------------------------------------------------------ live search (mocked HTTP)
def _fake_youtube_client(*, search_items, video_items, search_status=200, video_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            assert request.url.params["q"]
            assert request.url.params["type"] == "video"
            assert request.url.params["order"] == "viewCount"
            assert "publishedAfter" in request.url.params
            return httpx.Response(search_status, json={"items": search_items})
        if request.url.path.endswith("/videos"):
            return httpx.Response(video_status, json={"items": video_items})
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _search_items():
    return [
        {
            "id": {"videoId": "AAAAAAAAAAA"},
            "snippet": {"title": "Gaming clutch 1", "channelTitle": "Chan A"},
        },
        {
            "id": {"videoId": "BBBBBBBBBBB"},
            "snippet": {"title": "Gaming clutch 2", "channelTitle": "Chan B"},
        },
    ]


def _video_items():
    return [
        {
            "id": "AAAAAAAAAAA",
            "snippet": {
                "title": "Gaming clutch 1",
                "channelTitle": "Chan A",
                "channelId": "UCAAAAAAAAA",
                "publishedAt": "2026-11-21T10:00:00Z",
            },
            "statistics": {"viewCount": "120000", "likeCount": "9000", "commentCount": "800"},
        },
        {
            "id": "BBBBBBBBBBB",
            "snippet": {
                "title": "Gaming clutch 2",
                "channelTitle": "Chan B",
                "channelId": "UCBBBBBBBBB",
                "publishedAt": "2026-11-20T12:00:00Z",
            },
            "statistics": {"viewCount": "30000", "likeCount": "500", "commentCount": "40"},
        },
    ]


def test_search_trending_ranks_by_trend_score_with_live_fields():
    creds = TrendingCredentials(kind="api_key", token="<redacted>")
    client = _fake_youtube_client(search_items=_search_items(), video_items=_video_items())
    results = search_trending("gaming", credentials=creds, client=client)
    assert [v.video_id for v in results] == ["AAAAAAAAAAA", "BBBBBBBBBBB"]
    assert results[0].title == "Gaming clutch 1"
    assert results[0].channel_title == "Chan A"
    assert results[0].view_count == 120_000
    assert results[0].like_count == 9_000
    assert results[0].trend_score > results[1].trend_score
    assert results[0].url == "https://www.youtube.com/watch?v=AAAAAAAAAAA"


def test_search_trending_oauth_uses_bearer_header_not_key_param():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["has_key"] = "key" in request.url.params
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"items": _search_items()})
        return httpx.Response(200, json={"items": _video_items()})

    creds = TrendingCredentials(kind="oauth", token="oauth-token", account_name="My Channel")
    results = search_trending(
        "gaming", credentials=creds, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert seen["auth"] == "Bearer oauth-token"
    assert seen["has_key"] is False
    assert len(results) == 2


def test_search_trending_api_key_uses_key_param():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.url.params.get("key")
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"items": _search_items()})
        return httpx.Response(200, json={"items": _video_items()})

    creds = TrendingCredentials(kind="api_key", token="<redacted>")
    search_trending(
        "gaming", credentials=creds, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert seen["key"] == "<redacted>"


def test_search_trending_empty_result_is_empty_not_invented():
    client = _fake_youtube_client(search_items=[], video_items=[])
    results = search_trending(
        "gaming", credentials=TrendingCredentials(kind="api_key", token="k"), client=client
    )
    assert results == []


def test_search_trending_youtube_error_is_plain_language():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"message": "You have exceeded your quota."}},
        )

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    with pytest.raises(TrendingError, match="exceeded your quota"):
        search_trending(
            "gaming", credentials=TrendingCredentials(kind="api_key", token="k"), client=client
        )


def test_trending_video_to_dict_includes_url_and_score():
    video = TrendingVideo(
        video_id="AAAAAAAAAAA",
        title="T",
        channel_title="C",
        view_count=10,
        like_count=1,
        trend_score=42.5,
    )
    data = video.to_dict()
    assert data["url"] == "https://www.youtube.com/watch?v=AAAAAAAAAAA"
    assert data["trend_score"] == 42.5
    assert TRENDING_NOTICE.startswith("Trending shows what topics are working")
