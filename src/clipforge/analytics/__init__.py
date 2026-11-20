"""Analytics ingest + retention signals (research §4, §8).

All ingest functions are OPTIONAL and credential-gated: with no
credentials configured they return ``{"status": "not_configured", ...}``
and store nothing — the API stays dry-run safe. Live ingest needs:
- YouTube: YouTube Analytics API (``YOUTUBE_REFRESH_TOKEN``),
  ``creatorContentType=SHORTS`` for Shorts splits.
- Instagram/Facebook: IG/FB Insights via Graph API (``META_ACCESS_TOKEN``).
- TikTok: TikTok Display API (``TIKTOK_ACCESS_TOKEN``) — separate approval.

Snapshots are stored in :class:`clipforge.db.AnalyticsSnapshot` so the
scheduler / SEO loop can read them without re-calling the providers.

``retention_signals`` turns a snapshot into next-clip feedback: viewed%
(swiped-away), completion, and plain-language signals for the review UI.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from clipforge.db import AnalyticsSnapshot

YOUTUBE = "youtube"
INSTAGRAM = "instagram"
FACEBOOK = "facebook"
TIKTOK = "tiktok"


def _not_configured(platform: str, env_var: str) -> dict:
    return {
        "status": "not_configured",
        "platform": platform,
        "message": f"No credential: set {env_var} (and pass the provider's "
        "review/audit) to enable live analytics ingest.",
    }


def store_snapshot(
    session: Session,
    *,
    platform: str,
    account_id: str | None = None,
    views: int = 0,
    followers: int = 0,
    watch_hours: float = 0.0,
    shorts_views_90d: int = 0,
    completion_pct: float | None = None,
    viewed_pct: float | None = None,
    raw: dict | None = None,
) -> AnalyticsSnapshot:
    snap = AnalyticsSnapshot(
        platform=platform,
        account_id=account_id,
        views=views,
        followers=followers,
        watch_hours=watch_hours,
        shorts_views_90d=shorts_views_90d,
        completion_pct=completion_pct,
        viewed_pct=viewed_pct,
        raw_json=raw or {},
        date=datetime.now(UTC),
    )
    session.add(snap)
    session.commit()
    return snap


def ingest_youtube_analytics(session: Session, account_id: str | None = None) -> dict:
    """Stub: YouTube Analytics API. Returns not_configured without a token."""
    if not os.environ.get("YOUTUBE_REFRESH_TOKEN"):
        return _not_configured(YOUTUBE, "YOUTUBE_REFRESH_TOKEN")
    # Live implementation (post-review): call the Analytics API with
    # metrics=views,estimatedMinutesWatched,subscribersGained and
    # creatorContentType=SHORTS, then store_snapshot(...).
    return {
        "status": "not_implemented",
        "platform": YOUTUBE,
        "message": "YouTube Analytics ingest is a stub in v0.1.0 — "
        "credential present, API call not yet wired.",
    }


def ingest_meta_insights(
    session: Session, account_id: str | None = None, platform: str = INSTAGRAM
) -> dict:
    if not os.environ.get("META_ACCESS_TOKEN"):
        return _not_configured(platform, "META_ACCESS_TOKEN")
    return {
        "status": "not_implemented",
        "platform": platform,
        "message": "IG/FB Insights ingest is a stub in v0.1.0 — "
        "credential present, API call not yet wired.",
    }


def ingest_tiktok_display(session: Session, account_id: str | None = None) -> dict:
    if not os.environ.get("TIKTOK_ACCESS_TOKEN"):
        return _not_configured(TIKTOK, "TIKTOK_ACCESS_TOKEN")
    return {
        "status": "not_implemented",
        "platform": TIKTOK,
        "message": "TikTok Display API ingest is a stub in v0.1.0 — "
        "credential present, API call not yet wired.",
    }


def ingest_all(session: Session, account_id: str | None = None) -> list[dict]:
    return [
        ingest_youtube_analytics(session, account_id),
        ingest_meta_insights(session, account_id, INSTAGRAM),
        ingest_meta_insights(session, account_id, FACEBOOK),
        ingest_tiktok_display(session, account_id),
    ]


def retention_signals(snapshot: AnalyticsSnapshot) -> dict:
    """Next-clip feedback from one snapshot (simple, transparent rules).

    Signals (report §4 targets): completion >70% for <30s clips is good;
    viewed% (viewed-vs-swiped-away) is the Shorts feed's main signal.
    """
    signals: list[str] = []
    viewed = snapshot.viewed_pct
    completion = snapshot.completion_pct
    if viewed is not None:
        if viewed < 50:
            signals.append(
                "Low viewed% (<50%): hook in the first 1-3s is failing — "
                "re-cut the opening, put the payoff/keyword up front."
            )
        elif viewed >= 70:
            signals.append("Strong viewed% (>=70%): this hook works — reuse its pattern.")
    if completion is not None:
        if completion < 50:
            signals.append(
                "Low completion (<50%): viewers drop off — shorten, "
                "or move the payoff earlier; check the retention drop-off point."
            )
        elif completion >= 70:
            signals.append("High completion (>=70%): length and pacing are right for this topic.")
    if not signals:
        signals.append(
            "Not enough retention data yet — publish, then re-check "
            "viewed% / completion before cutting the next clip."
        )
    return {
        "platform": snapshot.platform,
        "views": snapshot.views,
        "viewed_pct": viewed,
        "completion_pct": completion,
        "signals": signals,
    }


__all__ = [
    "store_snapshot",
    "ingest_youtube_analytics",
    "ingest_meta_insights",
    "ingest_tiktok_display",
    "ingest_all",
    "retention_signals",
]
