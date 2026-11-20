"""Monetization tracker (research report §6).

Thresholds are DATA, not promises. No earnings are promised anywhere:
payouts depend on qualified views, originality, country, and platform
policy, none of which this module can guarantee.

Flagged prominently:
- YouTube: from 1 Feb 2027, NEW YPP applicants need 8,000 watch hours
  (was 4,000) or 20M Shorts views/90d (was 10M); 1,000 subs unchanged.
  Existing applicants must accept the new YPP terms by 31 Jan 2027.
- TikTok Creator Rewards: 10k followers + 100k views/30d, personal
  account, >=1-min original videos, 8 countries only (US, UK, DE, FR,
  JP, KR, MX, BR).
- Instagram Reels Play bonus ENDED 31 Aug 2025 — never promise it.
  Gifts need 500 followers; Subscriptions 10k.
- Facebook CMP: 10,000 followers per the CMP guide. Some 2026 guides
  cite 5,000 followers + 60,000 min watched/60d for legacy paths —
  UNVERIFIED; verify in Business Suite -> Monetization Eligibility.
"""

from __future__ import annotations

# program -> list of (metric_key, threshold, label)
THRESHOLDS: dict[str, list[dict]] = {
    "youtube": [
        {
            "program": "YPP Early (fan funding + Shopping)",
            "metrics": {
                "subscribers": 500,
                "watch_hours_12mo": 3000,
                "shorts_views_90d_alt": 3_000_000,
            },
            "note": "500 subs + 3 public uploads/90d + 3,000h/12mo OR 3M Shorts views/90d.",
        },
        {
            "program": "YPP Full (ads + Premium)",
            "metrics": {
                "subscribers": 1000,
                "watch_hours_12mo": 4000,
                "shorts_views_90d_alt": 10_000_000,
            },
            "note": "1,000 subs + 4,000h/12mo OR 10M Shorts views/90d. Shorts "
            "watch time does NOT count to the 4,000h. Shorts ad revenue "
            "pooled, 45% to creator.",
        },
        {
            "program": "YPP Full — NEW APPLICANTS FROM 1 FEB 2027",
            "metrics": {
                "subscribers": 1000,
                "watch_hours_12mo": 8000,
                "shorts_views_90d_alt": 20_000_000,
            },
            "note": "FLAG: thresholds DOUBLE for new applicants from 1 Feb 2027 "
            "(8,000h or 20M Shorts views). Accept new YPP terms by 31 Jan 2027.",
        },
    ],
    "tiktok": [
        {
            "program": "Creator Rewards Program",
            "metrics": {"followers": 10_000, "views_30d": 100_000},
            "note": "18+, personal account (not Business), 8 countries only "
            "(US, UK, DE, FR, JP, KR, MX, BR). Per video: original, "
            ">=1 min, >=1,000 qualified For You views. Short clips earn $0.",
        },
    ],
    "instagram": [
        {
            "program": "Gifts",
            "metrics": {"followers": 500},
            "note": "Pro account, 18+, payout account. $0.01 per star. "
            "Reels Play bonus ENDED 31 Aug 2025 — never promise per-view pay.",
        },
        {
            "program": "Subscriptions",
            "metrics": {"followers": 10_000},
            "note": "Pro account, 18+. Creator-set price.",
        },
    ],
    "facebook": [
        {
            "program": "Content Monetization Program (CMP)",
            "metrics": {"followers": 10_000},
            "note": "Page (not personal profile) or pro-mode profile. 10,000 "
            "followers per CMP guide — some guides cite 5,000 (UNVERIFIED). "
            "Reel must be >=10s to earn; views <5s excluded. In-stream Ads / "
            "Ads on Reels / Performance Bonus stopped earning 31 Aug 2025.",
        },
        {
            "program": "Stars",
            "metrics": {"followers": 500},
            "note": "500 followers + 1 live in 60d. $0.01 per star, $100 minimum.",
        },
    ],
}

# Map the caller's metric names onto the threshold keys above.
_METRIC_ALIASES = {
    "subs": "subscribers",
    "subscribers": "subscribers",
    "followers": "followers",
    "watch_hours": "watch_hours_12mo",
    "watch_hours_12mo": "watch_hours_12mo",
    "shorts_views": "shorts_views_90d_alt",
    "shorts_views_90d": "shorts_views_90d_alt",
    "shorts_views_90d_alt": "shorts_views_90d_alt",
    "views_30d": "views_30d",
    "views": "views_30d",
}


def _normalise(metrics: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in metrics.items():
        mapped = _METRIC_ALIASES.get(key)
        if mapped:
            out[mapped] = float(value or 0)
    return out


def progress(current_metrics: dict, platform: str | None = None) -> list[dict]:
    """Per-program progress: % to threshold + what's missing.

    ``current_metrics`` e.g. {"subscribers": 320, "watch_hours_12mo": 1200,
    "shorts_views_90d": 450000, "followers": 800, "views_30d": 12000}.
    Alt metrics (``*_alt``) are OR-paths: reaching the alt alone satisfies
    that leg. Returns one entry per program, shaped for the web contract:
    {platform, program, current, threshold, progress_pct, missing, note}.
    """
    current = _normalise(current_metrics)
    platforms = [platform] if platform else list(THRESHOLDS)
    rows: list[dict] = []
    for plat in platforms:
        for entry in THRESHOLDS.get(plat, []):
            thresholds: dict[str, float] = {k: float(v) for k, v in entry["metrics"].items()}
            # Progress = worst leg, where an alt leg counts if EITHER leg is met.
            legs: list[float] = []
            missing: list[str] = []
            for key, needed in thresholds.items():
                have = current.get(key, 0.0)
                if key.endswith("_alt"):
                    continue  # handled with its primary leg below
                legs.append(min(have / needed, 1.0) if needed else 1.0)
                if have < needed:
                    missing.append(f"{key}: {have:g}/{needed:g}")
            # Alt Shorts-views leg: for YPP, views OR hours satisfy the second leg.
            alt = thresholds.get("shorts_views_90d_alt")
            if alt is not None:
                have_alt = current.get("shorts_views_90d_alt", 0.0)
                have_hours = current.get("watch_hours_12mo", 0.0)
                need_hours = thresholds.get("watch_hours_12mo", 0.0)
                hours_leg = min(have_hours / need_hours, 1.0) if need_hours else 1.0
                alt_leg = min(have_alt / alt, 1.0)
                best = max(hours_leg, alt_leg)
                # Replace the hours leg (index 1) with the best of hours/views.
                if len(legs) > 1:
                    legs[1] = best
                if best < 1.0:
                    missing = [m for m in missing if not m.startswith("watch_hours")]
                    missing.append(
                        f"watch_hours_12mo: {have_hours:g}/{need_hours:g} OR "
                        f"shorts_views_90d: {have_alt:g}/{alt:g}"
                    )
            pct = round(100 * min(legs), 1) if legs else 0.0
            rows.append(
                {
                    "platform": plat,
                    "program": entry["program"],
                    "current": {k: current.get(k, 0.0) for k in thresholds},
                    "threshold": thresholds,
                    "progress_pct": pct,
                    "missing": missing,
                    "note": entry["note"],
                }
            )
    return rows


__all__ = ["THRESHOLDS", "progress"]
