"""Platform limits — VERIFY_LIVE before quoting or enforcing in production.

Every number below comes from the research report (§3, §4, §6), itself
drawn from official docs opened 20 Nov 2026. Limits change; do NOT
hard-code them anywhere else — import from here, and re-check live
(``content_publishing_limit`` for Instagram, Cloud Console quota for
YouTube) before relying on them.

Known conflicts / uncertainties — flagged, not silently resolved:
- YOUTUBE QUOTA CONFLICT: older docs say videos.insert costs 1,600 units
  (~6 uploads/day from the 10,000-unit pool); newer sources (Dec 2025
  cost cut + 1 Jun 2026 separate bucket) say videos.insert = 1 unit with a
  dedicated 100-uploads/day bucket and the 10k pool for other calls.
  VERIFY in Google Cloud Console; the code below records BOTH figures and
  defaults to the conservative one (6/day) for cap enforcement.
- FACEBOOK CMP followers: 10,000 per the CMP guide vs 5,000 cited in some
  2026 guides for legacy paths — UNVERIFIED (see monetization.py).
"""

from __future__ import annotations

# VERIFY_LIVE — re-check in the provider console / live endpoint before
# relying on any value in this file.
VERIFY_LIVE = True

PLATFORM_LIMITS: dict[str, dict] = {
    "youtube": {
        "max_duration_s": 180,  # Shorts = vertical file <=3 min (uploads after 15 Oct 2024)
        "min_duration_s": 0,
        "max_hashtags": 3,  # only 3 show above the title; use 2-3
        "title_max_chars": 100,
        "title_aim_chars": 60,
        "max_size_mb": 256,  # conservative; API accepts larger via resumable upload
        "daily_uploads_conservative": 6,  # 1,600 units x 6 = 9,600 of 10,000 pool
        "daily_uploads_new_bucket": 100,  # 1 unit/insert, dedicated bucket (Jun 2026) — VERIFY
        "quota_units_per_insert_old": 1600,
        "quota_units_per_insert_new": 1,  # VERIFY_LIVE — see conflict note above
        "quota_pool_units": 10000,
        "rate": "quota-based, resets midnight PT",
    },
    "instagram": {
        "max_duration_s": 900,  # API: 3s-15 min
        "min_duration_s": 3,
        "max_hashtags": 5,  # Dec 2025 cap (API docs still say 30 — app cap wins)
        "max_size_mb": 300,
        "max_width_px": 1920,
        "daily_posts": 100,  # Reels+feed+Stories combined, rolling 24h
        "calls_per_hour_per_user": 200,
        "note": "Professional (Business/Creator) accounts only; read "
        "content_publishing_limit live, do not hard-code.",
    },
    "facebook": {
        "max_duration_s": 90,  # API cap; in-app Reels have no length cap
        "min_duration_s": 3,
        "max_hashtags": 3,
        "daily_posts": 30,  # per Page, rolling 24h
        "min_width_px": 540,
        "min_height_px": 960,
    },
    "tiktok": {
        "max_duration_s": 600,  # per-account cap 3/5/10 min — check creator_info
        "min_duration_s": 0,
        "max_hashtags": 5,
        "max_size_mb": 4096,  # 4 GB
        "direct_post_rate_per_min": 6,  # per user token
        "upload_drafts_per_24h": 5,  # pending inbox drafts per user
        "note": "Unaudited apps: Direct Post forced SELF_ONLY (private), "
        "<=5 users/24h. Audit lifts limits.",
    },
}

# Daily publish caps used by the scheduler (conservative defaults).
DAILY_CAPS: dict[str, int] = {
    "youtube": PLATFORM_LIMITS["youtube"]["daily_uploads_conservative"],
    "instagram": PLATFORM_LIMITS["instagram"]["daily_posts"],
    "facebook": PLATFORM_LIMITS["facebook"]["daily_posts"],
    "tiktok": PLATFORM_LIMITS["tiktok"]["upload_drafts_per_24h"],
}

__all__ = ["PLATFORM_LIMITS", "DAILY_CAPS", "VERIFY_LIVE"]
