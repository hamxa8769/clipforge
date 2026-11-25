"""Unit tests — Creative Commons discovery + licence guardrail (v0.7.0).

No test hits the real YouTube API: every YouTube call goes through a
fake httpx client (MockTransport). Covers:

* ``search_trending(licensed_only=True)`` — passes
  ``videoLicense=creativeCommon`` to search.list, verifies each result
  via videos.list ``status.license`` and drops anything that is not
  Creative Commons; every kept result carries ``license`` +
  ``license_url``.
* ``search_trending(licensed_only=False)`` — v0.6.0 behaviour, no
  licence filtering, no ``videoLicense`` param.
* :func:`check_video_license` — CC, standard, own and unverifiable
  licences are all reported with ``can_clip=True`` (the hard block
  was removed at the user's explicit instruction after they checked
  YouTube's terms); standard and unverifiable licences carry a plain
  licence label in ``reason`` — no rights wording (v0.11.0).
"""

from __future__ import annotations

import httpx
import pytest

from clipforge.trending import (
    CC_LICENSE_URL,
    STANDARD_LICENSE_REASON,
    TrendingCredentials,
    TrendingError,
    check_video_license,
    license_url_for,
    search_trending,
)

CREDS = TrendingCredentials(kind="api_key", token="k")

CC_VIDEO = "CCCCCCCCCCC"
STD_VIDEO = "SSSSSSSSSSS"
OWN_VIDEO = "OOOOOOOOOOO"
NO_LICENSE_VIDEO = "NNNNNNNNNNN"

OWN_CHANNEL_ID = "UCOWNCHANNEL1"


def _video_item(video_id: str, *, license_: str | None, channel_id: str = "UCOTHERCHAN1"):
    item: dict = {
        "id": video_id,
        "snippet": {
            "title": f"Video {video_id}",
            "channelTitle": "Some Channel",
            "channelId": channel_id,
            "publishedAt": "2026-11-21T10:00:00Z",
        },
        "statistics": {"viewCount": "120000", "likeCount": "9000", "commentCount": "800"},
    }
    if license_ is not None:
        item["status"] = {"license": license_}
    return item


def _fake_client(*, search_ids, video_items, video_status=200):
    """Fake YouTube API: /search returns the ids, /videos returns the
    given items (each with whatever ``status.license`` the test set)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": {"videoId": vid},
                            "snippet": {"title": f"Video {vid}", "channelTitle": "Chan"},
                        }
                        for vid in search_ids
                    ]
                },
            )
        if request.url.path.endswith("/videos"):
            return httpx.Response(video_status, json={"items": video_items})
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ licence helpers
def test_license_url_for_cc_is_cc_by():
    assert license_url_for("creativeCommon") == CC_LICENSE_URL
    assert CC_LICENSE_URL.startswith("https://creativecommons.org/licenses/by/")


def test_license_url_for_standard_is_empty():
    assert license_url_for("youtube") == ""
    assert license_url_for("") == ""
    assert license_url_for("something-else") == ""


# ------------------------------------------------------------------ licensed_only search
def test_search_licensed_only_passes_video_license_param_and_keeps_cc():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            seen["video_license"] = request.url.params.get("videoLicense")
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": {"videoId": CC_VIDEO}, "snippet": {"title": "CC vid"}},
                        {"id": {"videoId": STD_VIDEO}, "snippet": {"title": "Std vid"}},
                    ]
                },
            )
        if request.url.path.endswith("/videos"):
            seen["parts"] = request.url.params.get("part")
            return httpx.Response(
                200,
                json={
                    "items": [
                        _video_item(CC_VIDEO, license_="creativeCommon"),
                        _video_item(STD_VIDEO, license_="youtube"),
                    ]
                },
            )
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    results = search_trending("gaming", credentials=CREDS, licensed_only=True, client=client)
    # search.list asked YouTube for CC only …
    assert seen["video_license"] == "creativeCommon"
    # … and the videos.list verification dropped the standard video
    # that slipped through anyway.
    assert [v.video_id for v in results] == [CC_VIDEO]
    assert results[0].license == "creativeCommon"
    assert results[0].license_url == CC_LICENSE_URL
    assert results[0].to_dict()["license"] == "creativeCommon"
    assert results[0].to_dict()["license_url"] == CC_LICENSE_URL
    # Verification needs the status part.
    assert "status" in (seen["parts"] or "")


def test_search_licensed_only_drops_result_with_no_licence_field():
    client = _fake_client(
        search_ids=[CC_VIDEO, NO_LICENSE_VIDEO],
        video_items=[
            _video_item(CC_VIDEO, license_="creativeCommon"),
            _video_item(NO_LICENSE_VIDEO, license_=None),
        ],
    )
    results = search_trending("gaming", credentials=CREDS, licensed_only=True, client=client)
    assert [v.video_id for v in results] == [CC_VIDEO]


def test_search_licensed_only_empty_when_nothing_is_cc():
    client = _fake_client(
        search_ids=[STD_VIDEO],
        video_items=[_video_item(STD_VIDEO, license_="youtube")],
    )
    results = search_trending("gaming", credentials=CREDS, licensed_only=True, client=client)
    assert results == []


def test_search_without_licensed_only_is_v060_behaviour():
    """Default: no videoLicense param, no status part, no filtering —
    exactly the v0.6.0 trending search."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            seen["video_license"] = request.url.params.get("videoLicense")
            return httpx.Response(
                200,
                json={"items": [{"id": {"videoId": STD_VIDEO}, "snippet": {"title": "Std"}}]},
            )
        if request.url.path.endswith("/videos"):
            seen["parts"] = request.url.params.get("part")
            return httpx.Response(200, json={"items": [_video_item(STD_VIDEO, license_="youtube")]})
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    results = search_trending("gaming", credentials=CREDS, client=client)
    assert seen["video_license"] is None
    assert seen["parts"] == "snippet,statistics"
    assert [v.video_id for v in results] == [STD_VIDEO]
    assert results[0].license == ""


# ------------------------------------------------------------------ check_video_license
def test_check_license_cc_video_can_clip():
    client = _fake_client(
        search_ids=[], video_items=[_video_item(CC_VIDEO, license_="creativeCommon")]
    )
    result = check_video_license(
        f"https://www.youtube.com/watch?v={CC_VIDEO}", credentials=CREDS, client=client
    )
    assert result["video_id"] == CC_VIDEO
    assert result["license"] == "creativeCommon"
    assert result["license_url"] == CC_LICENSE_URL
    assert result["can_clip"] is True
    assert "Creative Commons" in result["reason"]
    assert result["title"] == f"Video {CC_VIDEO}"
    assert result["channel"] == "Some Channel"


def test_check_license_standard_video_is_advisory_not_refused():
    """v0.9.1: a standard-licence video is reported and proceeds —
    can_clip is True, never a refusal. v0.11.0: the reason is a plain
    licence label, no rights wording."""
    client = _fake_client(search_ids=[], video_items=[_video_item(STD_VIDEO, license_="youtube")])
    result = check_video_license(f"https://youtu.be/{STD_VIDEO}", credentials=CREDS, client=client)
    assert result["license"] == "youtube"
    assert result["license_url"] == ""
    assert result["can_clip"] is True
    assert result["reason"] == STANDARD_LICENSE_REASON
    assert "standard licence" in result["reason"]
    assert "rights" not in result["reason"].lower()


def test_check_license_own_video_can_clip_even_when_standard():
    client = _fake_client(
        search_ids=[],
        video_items=[_video_item(OWN_VIDEO, license_="youtube", channel_id=OWN_CHANNEL_ID)],
    )
    result = check_video_license(
        f"https://www.youtube.com/watch?v={OWN_VIDEO}",
        credentials=CREDS,
        own_channel_id=OWN_CHANNEL_ID,
        client=client,
    )
    assert result["can_clip"] is True
    assert "your own video" in result["reason"].lower()


def test_check_license_third_party_standard_reports_advisory():
    client = _fake_client(
        search_ids=[],
        video_items=[_video_item(STD_VIDEO, license_="youtube", channel_id="UCOTHERCHAN1")],
    )
    result = check_video_license(
        f"https://www.youtube.com/watch?v={STD_VIDEO}",
        credentials=CREDS,
        own_channel_id=OWN_CHANNEL_ID,
        client=client,
    )
    assert result["can_clip"] is True
    assert result["channel_id"] == "UCOTHERCHAN1"
    assert "rights" not in result["reason"].lower()


def test_check_license_unverifiable_video_not_found_is_honest_error():
    client = _fake_client(search_ids=[], video_items=[])
    with pytest.raises(TrendingError, match="could not find that video"):
        check_video_license(
            f"https://www.youtube.com/watch?v={STD_VIDEO}", credentials=CREDS, client=client
        )


def test_check_license_no_licence_field_is_advisory_not_refused():
    """YouTube returned the video but no licence: report honestly
    and proceed — never fake a licence, never refuse."""
    client = _fake_client(search_ids=[], video_items=[_video_item(NO_LICENSE_VIDEO, license_=None)])
    result = check_video_license(
        f"https://www.youtube.com/watch?v={NO_LICENSE_VIDEO}", credentials=CREDS, client=client
    )
    assert result["license"] == ""
    assert result["can_clip"] is True
    assert "rights" not in result["reason"].lower()


def test_check_license_bad_url_is_honest_error():
    client = _fake_client(search_ids=[], video_items=[])
    with pytest.raises(TrendingError, match="doesn't look like a YouTube video"):
        check_video_license("https://example.com/not-youtube", credentials=CREDS, client=client)


def test_check_license_youtube_api_error_is_plain_language():
    client = _fake_client(search_ids=[], video_items=[], video_status=403)
    with pytest.raises(TrendingError):
        check_video_license(
            f"https://www.youtube.com/watch?v={STD_VIDEO}", credentials=CREDS, client=client
        )
