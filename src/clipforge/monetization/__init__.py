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

import json as _json
from datetime import date as _date
from datetime import timedelta as _timedelta
from pathlib import Path as _Path
from urllib.parse import parse_qsl as _parse_qsl
from urllib.parse import urlencode as _urlencode
from urllib.parse import urlparse as _urlparse
from urllib.parse import urlunparse as _urlunparse

import httpx as _httpx
from sqlalchemy.orm import Session as _Session

from clipforge import abtest as _abtest
from clipforge.db import CandidateSeo as _CandidateSeo
from clipforge.db import ClipCandidate as _ClipCandidate
from clipforge.db import Job as _Job
from clipforge.db import Post as _Post

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
    "x": [
        {
            "program": "Creator payouts (ad revenue share)",
            "metrics": {"followers": 500, "impressions_90d": 5_000_000},
            "note": "X Premium subscription + 500 followers + 5M organic "
            "impressions in the last 3 months (per X's published creator "
            "payouts rules). Ad-revenue share, not per-view pay.",
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
    "impressions_90d": "impressions_90d",
    "impressions": "impressions_90d",
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


# ------------------------------------------------------------------ v0.8.0
# Affiliate links, UTM tracking, A/B title+thumbnail results, revenue.
# Honesty rules for everything in this section: no earnings are ever
# invented (see the module docstring), views are only ever measured
# numbers (Growth snapshots / recorded A/B outcomes), and CTR is null
# until an impressions source exists — ClipForge reads views/likes/
# comments only, never impressions.
#: Platforms the revenue dashboard reports on, in display order.
REVENUE_PLATFORMS = ("youtube", "instagram", "facebook", "tiktok", "x")

#: Basis string used whenever a platform does not report earnings to ClipForge.
NOT_REPORTED_BASIS = "not reported by this platform's API"

#: Growth snapshot file (owned by ``clipforge_api.growth``) — the only
#: measured-views source the revenue dashboard and A/B results read.
_GROWTH_SNAPSHOT_FILENAME = "growth_snapshots.json"

#: A/B thumbnail sidecar (owned by this module, same pattern as
#: ``growth_snapshots.json``): {variant_id: thumbnail_url}. Thumbnails
#: are not a column on TitleVariant — adding one would need a migration
#: for existing databases — so they live here, keyed by variant id.
_AB_THUMBNAILS_FILENAME = "ab_thumbnails.json"

#: YouTube Analytics API — the only platform API ClipForge reads
#: earnings from (estimated revenue, channel==MINE).
YOUTUBE_ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"


class RevenueError(RuntimeError):
    """A YouTube Analytics revenue fetch failed (auth / network / API)."""


class MonetizationError(ValueError):
    """An affiliate / A/B request was invalid (bad URL, missing clip, ...)."""


# ------------------------------------------------------------------ UTM
def _require_http_url(url: str, *, what: str = "URL") -> str:
    """Validate an http(s) URL. Returns the stripped URL, else raises ValueError."""
    cleaned = (url or "").strip()
    parsed = _urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{what} must be an http(s) URL, got {url!r}")
    return cleaned


def add_utm(url: str, source: str, medium: str = "clip", campaign: str = "") -> str:
    """Append UTM params to a URL, preserving its existing query string.

    Produces ``?utm_source=<source>&utm_medium=<medium>&utm_campaign=<campaign>``
    (``medium`` defaults to ``"clip"``; ``campaign`` is the per-clip
    campaign, usually the clip id). Existing ``utm_*`` params are
    replaced, all other params are kept in order. Raises ValueError
    for a non-http(s) URL.
    """
    cleaned = _require_http_url(url, what="URL")
    if not (source or "").strip():
        raise ValueError("utm source is required")
    parsed = _urlparse(cleaned)
    params = [
        (k, v)
        for k, v in _parse_qsl(parsed.query, keep_blank_values=True)
        if not k.startswith("utm_")
    ]
    params.append(("utm_source", source.strip()))
    params.append(("utm_medium", (medium or "clip").strip()))
    if (campaign or "").strip():
        params.append(("utm_campaign", campaign.strip()))
    return _urlunparse(parsed._replace(query=_urlencode(params)))


def validate_affiliate_url(url: str) -> str:
    """Validate an affiliate link. Returns the stripped URL.

    Raises :class:`MonetizationError` (a ValueError) for a bad URL.
    """
    try:
        return _require_http_url(url, what="Affiliate URL")
    except ValueError as exc:
        raise MonetizationError(str(exc)) from exc


# ------------------------------------------------------------------ affiliate links
def attach_affiliate_link(db: _Session, clip_id: str, url: str) -> dict:
    """Store an affiliate link on a clip and append it to its descriptions.

    Storage (existing stores only, no new tables):
    - ``Job.meta_json["affiliate_links"][clip_id]`` — the canonical record.
    - Every stored SEO row for the clip (``CandidateSeo``): the UTM-tagged
      link is appended to ``seo["description"]`` (once per platform) and
      ``seo["affiliate_url"]`` is set to the raw link.
    - Every not-yet-published Post for the clip: same append to its SEO
      description, so the link goes out with the next publish.
    Published posts are left untouched — their description already went out.

    Raises :class:`MonetizationError` for an unknown clip or a bad URL.
    Returns ``{"clip_id", "affiliate_url", "descriptions_updated", "platforms"}``.
    """
    cleaned = validate_affiliate_url(url)
    clip = db.get(_ClipCandidate, clip_id)
    if clip is None:
        raise MonetizationError(f"clip {clip_id!r} not found")
    job = db.get(_Job, clip.job_id)
    if job is not None:
        meta = dict(job.meta_json or {})
        links = dict(meta.get("affiliate_links") or {})
        links[clip_id] = cleaned
        meta["affiliate_links"] = links
        job.meta_json = meta
    updated = 0
    platforms: list[str] = []
    for row in db.query(_CandidateSeo).filter_by(clip_candidate_id=clip_id).all():
        seo = dict(row.seo_json or {})
        tagged = add_utm(cleaned, row.platform, "clip", clip_id)
        description = str(seo.get("description") or "")
        if tagged not in description:
            seo["description"] = (description + "\n\n" + tagged).strip()
        seo["affiliate_url"] = cleaned
        row.seo_json = seo
        updated += 1
        platforms.append(row.platform)
    for post in (
        db.query(_Post)
        .filter_by(clip_candidate_id=clip_id)
        .filter(_Post.status.in_(("draft", "pending_approval", "approved", "scheduled")))
        .all()
    ):
        seo = dict(post.seo_json or {})
        tagged = add_utm(cleaned, post.platform, "clip", clip_id)
        description = str(seo.get("description") or seo.get("caption") or "")
        if tagged not in description:
            seo["description"] = (description + "\n\n" + tagged).strip()
        seo["affiliate_url"] = cleaned
        post.seo_json = seo
        updated += 1
        if post.platform not in platforms:
            platforms.append(post.platform)
    db.commit()
    return {
        "clip_id": clip_id,
        "affiliate_url": cleaned,
        "descriptions_updated": updated,
        "platforms": platforms,
    }


# ------------------------------------------------------------------ A/B thumbnails (sidecar)
def _thumbnail_path(data_dir: str | _Path) -> _Path:
    return _Path(data_dir) / _AB_THUMBNAILS_FILENAME


def load_variant_thumbnails(data_dir: str | _Path) -> dict[str, str]:
    """{variant_id: thumbnail_url} from the sidecar file ({} when none)."""
    try:
        raw = _json.loads(_thumbnail_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_variant_thumbnail(data_dir: str | _Path, variant_id: str, thumbnail: str) -> None:
    """Store a variant's thumbnail URL. Raises ValueError for a bad URL."""
    cleaned = _require_http_url(thumbnail, what="Thumbnail URL")
    path = _thumbnail_path(data_dir)
    data = load_variant_thumbnails(data_dir)
    data[variant_id] = cleaned
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(data, indent=2), encoding="utf-8")


def variant_thumbnail(data_dir: str | _Path, variant_id: str) -> str | None:
    """A variant's stored thumbnail URL, or None when none was saved."""
    value = load_variant_thumbnails(data_dir).get(variant_id)
    return value if isinstance(value, str) else None


# ------------------------------------------------------------------ A/B variants
def create_ab_variants(
    db: _Session,
    clip_id: str,
    variants: list[dict],
    data_dir: str | _Path,
) -> list[dict]:
    """Create A/B title (+optional thumbnail) variants for a clip.

    ``variants`` is ``[{"title": ..., "thumbnail": ...?, "platform": ...?}]``
    (platform defaults to ``"youtube"``). Titles are stored with the
    existing :mod:`clipforge.abtest` store; thumbnails go in the
    sidecar file (see :func:`save_variant_thumbnail`). The first
    variant becomes the control when none is marked as one.
    Raises :class:`MonetizationError` for an unknown clip, an empty
    variant list, an empty title, or a bad thumbnail URL. Returns the
    created variants as ``ab_results_for_clip`` rows (views 0, not
    measured yet).
    """
    if db.get(_ClipCandidate, clip_id) is None:
        raise MonetizationError(f"clip {clip_id!r} not found")
    if not variants:
        raise MonetizationError("at least one variant is required")
    if len(variants) > 10:
        raise MonetizationError("at most 10 variants per clip")
    created = []
    for index, raw in enumerate(variants):
        title = str((raw or {}).get("title") or "").strip()
        if not title:
            raise MonetizationError(f"variant {index + 1} needs a title")
        platform = str((raw or {}).get("platform") or "youtube").strip()
        if platform not in ("youtube", "instagram", "facebook", "tiktok", "x"):
            raise MonetizationError(f"unknown platform for variant {index + 1}: {platform!r}")
        thumbnail = (raw or {}).get("thumbnail")
        if thumbnail is not None and str(thumbnail).strip():
            try:
                _require_http_url(str(thumbnail), what="Thumbnail URL")
            except ValueError as exc:
                raise MonetizationError(str(exc)) from exc
        variant = _abtest.create_variant(
            db,
            clip_candidate_id=clip_id,
            platform=platform,
            title=title,
            is_control=bool((raw or {}).get("is_control")) or index == 0,
        )
        if thumbnail is not None and str(thumbnail).strip():
            save_variant_thumbnail(data_dir, variant.id, str(thumbnail).strip())
        created.append(variant)
    return ab_results_for_clip(db, clip_id, data_dir)


def _load_growth_snapshots(data_dir: str | _Path) -> dict:
    """Growth per-video stats: {video_id: {views, likes, comments, ...}}."""
    try:
        raw = _json.loads((_Path(data_dir) / _GROWTH_SNAPSHOT_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def ab_results_for_clip(db: _Session, clip_id: str, data_dir: str | _Path) -> list[dict]:
    """A/B results for a clip: variants with views / CTR where measured.

    Views come from the recorded A/B outcome when one exists, else
    from the Growth snapshot for a post that used the variant (via
    ``Post.title_variant_id`` → ``publish_result.provider_post_id``).
    When neither exists, views are 0 and ``measured`` is False —
    "not measured yet", never an invented number. ``ctr`` is always
    null: ClipForge reads views/likes/comments only, never
    impressions, so CTR cannot be computed from any source it has.
    """
    thumbnails = load_variant_thumbnails(data_dir)
    snapshots = _load_growth_snapshots(data_dir)
    # provider_post_id -> snapshot views, for posts that used a variant.
    snapshot_views: dict[str, int] = {}
    for post in db.query(_Post).filter_by(clip_candidate_id=clip_id).all():
        if not post.title_variant_id:
            continue
        provider_id = (post.publish_result_json or {}).get("provider_post_id")
        if provider_id and isinstance(snapshots.get(provider_id), dict):
            try:
                snapshot_views[post.title_variant_id] = int(
                    snapshots[provider_id].get("views") or 0
                )
            except (TypeError, ValueError):
                continue
    rows: list[dict] = []
    for variant in _abtest.list_variants(db, clip_id):
        recorded = variant.outcome_updated_at is not None
        if recorded:
            views = int(variant.views or 0)
            measured = True
        elif variant.id in snapshot_views:
            views = snapshot_views[variant.id]
            measured = True
        else:
            views = 0
            measured = False
        counts = _abtest.assignment_counts(db, clip_id, variant.platform)
        rows.append(
            {
                "id": variant.id,
                "clip_candidate_id": variant.clip_candidate_id,
                "platform": variant.platform,
                "title": variant.title,
                "thumbnail": thumbnails.get(variant.id),
                "is_control": bool(variant.is_control),
                "assigned_posts": counts.get(variant.id, 0),
                "views": views,
                "ctr": None,
                "measured": measured,
                "views_basis": (
                    "recorded A/B outcome"
                    if recorded
                    else (
                        "Growth stats for the posted clip"
                        if variant.id in snapshot_views
                        else "not measured yet"
                    )
                ),
                "ctr_basis": (
                    "not measured yet — ClipForge reads views/likes/comments "
                    "only, not impressions, so CTR cannot be computed"
                ),
                "created_at": variant.created_at.isoformat() if variant.created_at else None,
            }
        )
    return rows


# ------------------------------------------------------------------ revenue
def fetch_youtube_revenue(
    access_token: str,
    *,
    client: _httpx.Client | None = None,
    days: int = 28,
) -> dict:
    """YouTube Analytics estimated revenue for the connected channel.

    ``GET /v2/reports?ids=channel==MINE&metrics=views,estimatedRevenue``
    over the last ``days`` days. Returns ``{"views", "earnings",
    "currency": "USD", "days"}``. Raises :class:`RevenueError` on any
    failure — the caller reports earnings as not reported then, never
    a guessed number. Note the current ClipForge YouTube OAuth scopes
    (``youtube.upload`` + ``youtube.readonly``) do NOT include the
    Analytics scope, so a 403 here is expected until the YouTube
    connection is re-granted with Analytics access.
    """
    if days < 1:
        raise RevenueError("days must be >= 1")
    end = _date.today()
    start = end - _timedelta(days=days)
    own_client = client is None
    client = client or _httpx.Client(timeout=15.0, trust_env=False)
    try:
        resp = client.get(
            YOUTUBE_ANALYTICS_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "ids": "channel==MINE",
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "metrics": "views,estimatedRevenue",
            },
        )
    except _httpx.HTTPError as exc:
        raise RevenueError(f"Could not reach YouTube Analytics: {exc}") from exc
    finally:
        if own_client:
            client.close()
    if resp.status_code == 403:
        raise RevenueError(
            "YouTube Analytics refused the request (HTTP 403) — the connected "
            "YouTube account did not grant Analytics access (the current "
            "ClipForge YouTube connection requests upload/read-only scopes only)."
        )
    if resp.status_code != 200:
        raise RevenueError(f"YouTube Analytics returned HTTP {resp.status_code}.")
    try:
        rows = resp.json().get("rows") or []
    except ValueError as exc:
        raise RevenueError("YouTube Analytics returned an unreadable response.") from exc
    if not rows:
        raise RevenueError("YouTube Analytics returned no rows for the last 28 days.")
    try:
        views = int(rows[0][0])
        earnings = float(rows[0][1])
    except (TypeError, ValueError, IndexError) as exc:
        raise RevenueError("YouTube Analytics returned rows in an unexpected shape.") from exc
    return {"views": views, "earnings": round(earnings, 2), "currency": "USD", "days": days}


def _measured_views_by_platform(db: _Session, data_dir: str | _Path) -> dict[str, int]:
    """Measured views per platform from Growth snapshots of published posts."""
    snapshots = _load_growth_snapshots(data_dir)
    totals: dict[str, int] = {}
    for post in db.query(_Post).filter_by(status="published").all():
        provider_id = (post.publish_result_json or {}).get("provider_post_id")
        if not provider_id or not isinstance(snapshots.get(provider_id), dict):
            continue
        try:
            views = int(snapshots[provider_id].get("views") or 0)
        except (TypeError, ValueError):
            continue
        totals[post.platform] = totals.get(post.platform, 0) + views
    return totals


def revenue_rows(
    db: _Session,
    *,
    data_dir: str | _Path,
    youtube_access_token: str | None = None,
    client: _httpx.Client | None = None,
) -> list[dict]:
    """Revenue dashboard rows, one per platform.

    ``views`` are measured Growth-stats totals (0 when nothing was ever
    measured). ``earnings`` / ``rpm`` are real only for YouTube when a
    connected token is supplied AND the Analytics call succeeds;
    everywhere else they are null with ``basis`` =
    ``"not reported by this platform's API"`` — never invented numbers.
    """
    measured = _measured_views_by_platform(db, data_dir)
    rows: list[dict] = []
    for platform in REVENUE_PLATFORMS:
        views = int(measured.get(platform, 0))
        row: dict = {
            "platform": platform,
            "views": views,
            "rpm": None,
            "earnings": None,
            "currency": "",
            "basis": NOT_REPORTED_BASIS,
            "views_basis": (
                "measured ClipForge stats (Growth snapshots of published posts)"
                if views > 0 or platform in measured
                else "no posted clips with measured stats yet"
            ),
        }
        if platform == "youtube" and youtube_access_token:
            try:
                report = fetch_youtube_revenue(youtube_access_token, client=client, days=28)
            except RevenueError as exc:
                row["basis"] = (
                    f"YouTube Analytics request failed — earnings are not reported. {exc}"
                )
            else:
                earnings = float(report["earnings"])
                row["earnings"] = earnings
                row["currency"] = str(report["currency"])
                row["views"] = int(report["views"])
                row["rpm"] = round(earnings / row["views"] * 1000, 2) if row["views"] > 0 else None
                row["basis"] = (
                    "YouTube Analytics API — estimated revenue, last 28 days (channel==MINE)"
                )
                row["views_basis"] = "YouTube Analytics API, last 28 days"
        rows.append(row)
    return rows


__all__ += [
    "NOT_REPORTED_BASIS",
    "REVENUE_PLATFORMS",
    "MonetizationError",
    "RevenueError",
    "YOUTUBE_ANALYTICS_URL",
    "ab_results_for_clip",
    "add_utm",
    "attach_affiliate_link",
    "create_ab_variants",
    "fetch_youtube_revenue",
    "load_variant_thumbnails",
    "revenue_rows",
    "save_variant_thumbnail",
    "validate_affiliate_url",
    "variant_thumbnail",
]
