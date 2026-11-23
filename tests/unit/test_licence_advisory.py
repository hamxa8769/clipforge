"""Advisory licence contract (v0.9.1) — one place asserting the whole
change end to end at the unit level.

The v0.7.0 hard block (standard / unknown licences refused with 409 /
``skipped_licence`` / 422) was removed at the user's explicit
instruction (2026-11-23) after they checked YouTube's terms and
confirmed clipping is officially allowed. The licence is now reported,
never enforced:

* every licence value proceeds (``can_clip`` is always True);
* the rights notice (:data:`clipforge.licensing.RIGHTS_ADVISORY`) is
  shown instead of a refusal, and says the user is responsible;
* CC sources still get their attribution block, idempotently.
"""

from __future__ import annotations

import httpx

from clipforge.licensing import (
    RIGHTS_ADVISORY,
    append_attribution,
    build_attribution_text,
    publish_blocked_reason,
)
from clipforge.live import check_can_clip, clip_decision
from clipforge.trending import TrendingCredentials, check_video_license

CREDS = TrendingCredentials(kind="api_key", token="k")
STD_VIDEO = "SSSSSSSSSSS"


def _fake_client(license_: str | None):
    def handler(request: httpx.Request) -> httpx.Response:
        item: dict = {
            "id": STD_VIDEO,
            "snippet": {
                "title": "Video",
                "channelTitle": "Some Channel",
                "channelId": "UCOTHERCHAN1",
                "publishedAt": "2026-11-21T10:00:00Z",
            },
            "statistics": {"viewCount": "100", "likeCount": "1", "commentCount": "0"},
        }
        if license_ is not None:
            item["status"] = {"license": license_}
        return httpx.Response(200, json={"items": [item]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_rights_advisory_says_the_user_is_responsible():
    assert "Make sure you have the right to use this video" in RIGHTS_ADVISORY
    assert "You are responsible for the rights to what you post" in RIGHTS_ADVISORY
    # Never claims the app guarantees rights clearance.
    assert "guarantee" not in RIGHTS_ADVISORY.lower()


def test_no_licence_value_blocks_publishing():
    for licence in ("standard", "cc-by-4.0", "cc0", "public-domain", "own", None):
        assert publish_blocked_reason(licence) is None


def test_licence_check_is_advisory_for_every_licence():
    for license_ in ("creativeCommon", "youtube", None):
        result = check_video_license(
            f"https://www.youtube.com/watch?v={STD_VIDEO}",
            credentials=CREDS,
            client=_fake_client(license_),
        )
        assert result["can_clip"] is True, license_
    # Standard and unknown licences carry the rights notice.
    std = check_video_license(
        f"https://www.youtube.com/watch?v={STD_VIDEO}",
        credentials=CREDS,
        client=_fake_client("youtube"),
    )
    assert RIGHTS_ADVISORY in std["reason"]
    unknown = check_video_license(
        f"https://www.youtube.com/watch?v={STD_VIDEO}",
        credentials=CREDS,
        client=_fake_client(None),
    )
    assert RIGHTS_ADVISORY in unknown["reason"]


def test_live_clip_decision_is_advisory_for_every_licence():
    for licence in ("creativeCommon", "youtube", "unknown", ""):
        can_clip, reason = clip_decision(licence, is_own=False)
        assert can_clip is True, licence
        check_can_clip(licence, False)  # never raises for a licence
    assert RIGHTS_ADVISORY in clip_decision("youtube", is_own=False)[1]


def test_cc_attribution_still_auto_added_idempotently():
    text = build_attribution_text(
        "Sunset Timelapse",
        "Free Nature Films",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "cc-by-3.0",
    )
    assert "Licensed under" in text
    desc = append_attribution("A great clip", text)
    assert append_attribution(desc, text).count(text) == 1
    # Standard-licence sources carry no CC block to add.
    assert build_attribution_text("T", "C", "https://example.test/v", "standard") == ""
