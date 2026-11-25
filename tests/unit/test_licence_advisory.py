"""Licence contract (v0.11.0) — one place asserting the whole change
end to end at the unit level.

The v0.7.0 hard block (standard / unknown licences refused with 409 /
``skipped_licence`` / 422) was removed at the user's explicit
instruction (2026-11-23) after they checked YouTube's terms and
confirmed clipping is officially allowed. v0.11.0 (2026-11-25, same
instruction) removed the remaining notices as well:

* every licence value proceeds (``can_clip`` is always True);
* no rights / licence notice exists anywhere — the
  ``RIGHTS_ADVISORY`` constant is deleted, and no reason string
  carries rights wording;
* CC sources still get their attribution block from the helpers,
  idempotently (the SEO step appends it; the UI no longer shows it).
"""

from __future__ import annotations

import httpx

from clipforge.licensing import (
    append_attribution,
    build_attribution_text,
    publish_blocked_reason,
)
from clipforge.live import check_can_clip, clip_decision
from clipforge.trending import TrendingCredentials, check_video_license

CREDS = TrendingCredentials(kind="api_key", token="<redacted>")
STD_VIDEO = "SSSSSSSSSSS"

#: Rights wording that must not appear in any licence reason any more.
_FORBIDDEN = (
    "you are responsible for the rights",
    "make sure you have the right",
    "rights notice",
)


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
    # Standard and unknown licences carry a plain label — no rights
    # wording anywhere in the reason.
    std = check_video_license(
        f"https://www.youtube.com/watch?v={STD_VIDEO}",
        credentials=CREDS,
        client=_fake_client("youtube"),
    )
    assert std["reason"] == "This video uses YouTube's standard licence."
    assert all(w not in std["reason"].lower() for w in _FORBIDDEN)
    unknown = check_video_license(
        f"https://www.youtube.com/watch?v={STD_VIDEO}",
        credentials=CREDS,
        client=_fake_client(None),
    )
    assert unknown["reason"] == "YouTube did not report a licence for this video."
    assert all(w not in unknown["reason"].lower() for w in _FORBIDDEN)


def test_live_clip_decision_is_advisory_for_every_licence():
    for licence in ("creativeCommon", "youtube", "unknown", ""):
        can_clip, reason = clip_decision(licence, is_own=False)
        assert can_clip is True, licence
        check_can_clip(licence, False)  # never raises for a licence
        assert all(w not in reason.lower() for w in _FORBIDDEN)
    assert clip_decision("youtube", is_own=False)[1] == (
        "This live stream uses the standard YouTube licence."
    )


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
