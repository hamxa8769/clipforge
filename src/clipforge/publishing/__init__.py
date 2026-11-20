"""Publishing adapters — official APIs only, dry-run by default.

- :mod:`clipforge.publishing.youtube` — YouTube Data API v3 videos.insert
- :mod:`clipforge.publishing.meta` — Instagram Graph API v26 + Facebook Reels
- :mod:`clipforge.publishing.tiktok` — TikTok Content Posting API
- :mod:`clipforge.publishing.limits` — limits (VERIFY_LIVE) — never hard-code
- :mod:`clipforge.publishing.base` — PublishResult, rate guards, dry-run

No Selenium, no cookie uploaders, no unofficial TikTok web API.
"""

from __future__ import annotations

from clipforge.publishing import meta, tiktok, youtube
from clipforge.publishing.base import (
    NotApprovedError,
    NotConfiguredError,
    PublishResult,
    RateLimitError,
)
from clipforge.publishing.limits import DAILY_CAPS, PLATFORM_LIMITS

__all__ = [
    "youtube",
    "meta",
    "tiktok",
    "PublishResult",
    "NotConfiguredError",
    "NotApprovedError",
    "RateLimitError",
    "PLATFORM_LIMITS",
    "DAILY_CAPS",
]
