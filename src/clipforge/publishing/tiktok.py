"""TikTok publisher — TikTok Content Posting API (official only).

Two modes (report §3):
- Upload-to-inbox (``video.upload``) — DEFAULT pre-audit path. Sends the
  video to the creator's TikTok inbox as a draft; the creator finishes /
  publishes in-app. No audit needed. <=5 pending drafts / user / 24h.
- Direct Post (``video.publish``) — publishes straight to the profile.
  GATED: only when the app has passed TikTok's audit (``audited=True``)
  AND the operator explicitly opts in (``TIKTOK_DIRECT_POST_OPT_IN=true``
  env). Unaudited apps: all Direct Posts are forced SELF_ONLY (private),
  posting account must itself be private, <=5 users/24h. Never bypass
  this gate — an in-house tool for your own accounts fails the audit,
  and the app must be for a wide audience.

Posting-screen rules the API caller must honour (Content Sharing
Guidelines): show creator nickname, check max duration
(``creator_info.max_video_post_duration_sec``), privacy dropdown with NO
default (creator must pick — ``privacy_level`` is required for live),
Duet/Stitch/Comment unchecked by default, commercial-content + music-usage
declaration, preview + consent, no watermarks, ``is_aigc`` flag for
AI-generated content.

Rate: Direct Post 6 req/min per user token. No Selenium, no cookie
uploaders, no unofficial TikTok web API — ever.
"""

from __future__ import annotations

import os

from clipforge.publishing.base import (
    NotConfiguredError,
    PublishResult,
    dry_run_result,
    rate_guard,
    require_live,
    utcnow,
)
from clipforge.publishing.limits import PLATFORM_LIMITS

PLATFORM = "tiktok"
API_BASE = "https://open.tiktokapis.com/v2"


def _configured() -> bool:
    return bool(os.environ.get("TIKTOK_ACCESS_TOKEN"))


def _check_privacy(privacy_level: str | None, dry_run: bool) -> list[str]:
    warnings: list[str] = []
    if privacy_level is None:
        warnings.append(
            "privacy_level has NO default (TikTok Content Sharing Guidelines): "
            "the creator must pick PUBLIC_TO_EVERYONE / MUTUAL_FOLLOW_FRIENDS / "
            "SELF_ONLY explicitly. Live publish refuses without it."
        )
    elif privacy_level not in {"PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"}:
        raise ValueError(f"invalid privacy_level {privacy_level!r}")
    return warnings


def publish_tiktok(
    *,
    title: str,
    video_url: str | None = None,
    video_path: str | None = None,
    mode: str = "upload",  # "upload" (inbox draft, default) | "direct"
    audited: bool = False,
    privacy_level: str | None = None,
    is_aigc: bool = True,  # AI-clipped content: label it
    disable_duet: bool = True,
    disable_stitch: bool = True,
    disable_comment: bool = True,
    duration_s: float | None = None,
    dry_run: bool = True,
) -> PublishResult:
    if mode not in {"upload", "direct"}:
        raise ValueError("mode must be 'upload' (inbox draft) or 'direct'")
    if mode == "direct" and not audited:
        raise NotConfiguredError(
            "TikTok Direct Post requires a passed audit (audited=True). "
            "Unaudited apps get SELF_ONLY private posts only. Use mode='upload' "
            "(inbox draft) until the audit passes."
        )
    if mode == "direct" and os.environ.get("TIKTOK_DIRECT_POST_OPT_IN", "").lower() != "true":
        raise NotConfiguredError(
            "TikTok Direct Post also needs explicit opt-in: set "
            "TIKTOK_DIRECT_POST_OPT_IN=true after the audit passes."
        )
    warnings = _check_privacy(privacy_level, dry_run)
    if mode == "direct" and privacy_level is None and not dry_run:
        raise ValueError("privacy_level is required for a live Direct Post (no default).")
    if duration_s is not None and duration_s > PLATFORM_LIMITS[PLATFORM]["max_duration_s"]:
        raise ValueError(f"TikTok max video is 10 min; clip is {duration_s:.0f}s.")

    source = "PULL_FROM_URL" if video_url else "FILE_UPLOAD"
    endpoint = "video.upload" if mode == "upload" else "video.publish"
    payload: dict = {
        "method": "POST",
        "url": f"{API_BASE}/post/publish/{endpoint}/",
        "mode": mode,
        "post_info": {
            "title": title,
            "privacy_level": privacy_level or "<creator must pick — no default>",
            "disable_duet": disable_duet,
            "disable_stitch": disable_stitch,
            "disable_comment": disable_comment,
            "is_aigc": is_aigc,
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
        },
        "source_info": {"source": source, "video_url": video_url}
        if video_url
        else {"source": source},
        "warnings": warnings,
        "note": (
            "Upload-to-inbox: draft lands in the creator's TikTok inbox; "
            "creator finishes/publishes in-app. No audit needed."
            if mode == "upload"
            else "Direct Post: publishes to the profile immediately (audited apps only)."
        ),
    }
    if dry_run:
        return dry_run_result(
            PLATFORM,
            payload,
            f"TikTok {endpoint} ({mode}). Guards: 6 req/min Direct Post, "
            "<=5 inbox drafts/24h. is_aigc=True labels AI-clipped content.",
        )
    require_live(dry_run, _configured(), "TikTok publish")
    rate_guard.check("tiktok:direct", 6, 60)
    import requests

    token = os.environ["TIKTOK_ACCESS_TOKEN"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    resp = requests.post(payload["url"], json=payload, headers=headers, timeout=120).json()
    data = resp.get("data", {})
    if resp.get("error", {}).get("code") not in (None, "ok"):
        return PublishResult(
            platform=PLATFORM, status="failed", dry_run=False, payload=payload, error=str(resp)
        )
    return PublishResult(
        platform=PLATFORM,
        status="draft_created" if mode == "upload" else "published",
        dry_run=False,
        provider_post_id=str(data.get("publish_id") or ""),
        payload=payload,
        message=(
            "Draft sent to TikTok inbox — finish in-app."
            if mode == "upload"
            else "Published to TikTok."
        ),
        published_at=utcnow() if mode == "direct" else None,
    )


__all__ = ["publish_tiktok", "PLATFORM", "API_BASE"]
