"""Growth view (v0.6.0, Worker C).

Monitors posted clips and turns real numbers into plain, actionable
advice. Reads only records that already exist — no schema change:

Posted-clip contract (from the jobs / queue / posts store):
  posted_video_id = Post.publish_result_json["provider_post_id"]
                    (set by the publishing adapters on a live publish)
  platform        = Post.platform
  title           = Post.seo_json["title"] (fallback: caption/description)
  posted_at       = Post.published_at (fallback: Post.created_at)
  niche           = Job.meta_json["niche"] via ClipCandidate.job_id
                    (Agent jobs store it; "" when unknown)

Only posts with status ``published`` count as posted. A ``dry_run``
post was never sent anywhere, so it is not a posted clip.

Stats fetching (v0.6.0: YouTube only):
  YouTube Data API v3 ``videos.list(part=statistics, id=...)`` —
  views / likes / comments, 1 quota unit per call, up to 50 ids batched.
  Auth: the connected YouTube OAuth access token (TokenStore) when the
  channel is connected and the token is not expired, else the
  ``YOUTUBE_API_KEY`` env var (public videos only). No token and no key
  -> stats are "not_connected" and the page says so plainly.
  Instagram / Facebook / TikTok have no per-video stats endpoint wired
  in ClipForge yet -> "unsupported" with the reason, never fake numbers.

Snapshots: each successful fetch is stored in
``<data_dir>/growth_snapshots.json`` (owned by this module, same pattern
as ``setup.json``). The previous snapshot is what makes the per-clip
trend honest: without a "before" number there is no "growing" — the
clip is labelled "new" until a second fetch exists.

HONESTY RULE (enforced here, not just in the UI): every advice item
carries ``basis`` — ``"own_data"`` with the clip count it rests on, or
``"general"`` for documented YouTube best practice. Own-data advice
needs at least MIN_CLIPS_FOR_ADVICE posted clips *with stats*; below
that the endpoint returns the general guidance and says plainly there
is not enough of the user's own data yet. Nothing is ever invented.

Endpoints (registered in ``clipforge_api/app.py`` — one include line,
same pattern as the setup router):
  GET  /growth          full view: clips + totals + advice + status
  POST /growth/refresh  re-fetch stats now, store a snapshot, return /growth
  GET  /growth/advice   advice only (no live fetch; uses stored snapshots)
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from clipforge.auth.secrets import SecretStore
from clipforge.auth.token_store import TokenStore
from clipforge.config import settings
from clipforge.db import ClipCandidate, Job, Post, init_db, session_factory

router = APIRouter(prefix="/growth", tags=["growth"])

_engine = init_db()
_Session = session_factory()


def get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


def _data_dir() -> Path:
    # Same resolution as clipforge_api.app._data_dir(): the env override
    # wins so tests can point at a temp dir; otherwise the configured
    # data dir. Local import-free: settings is already imported above.
    return Path(os.environ.get("CLIPFORGE_DATA_DIR") or settings.data_dir)


# ------------------------------------------------------------------ constants
MIN_CLIPS_FOR_ADVICE = 5  # posted clips WITH stats needed for own-data advice
YOUTUBE_STATS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_BATCH_SIZE = 50
SNAPSHOT_FILENAME = "growth_snapshots.json"
# A previous snapshot younger than this cannot show a trend yet.
TREND_MIN_GAP = timedelta(hours=1)

SUPPORTED_STATS_PLATFORMS = {"youtube"}
# Platforms ClipForge can publish to but cannot yet read per-video stats for.
UNSUPPORTED_STATS_REASON = (
    "Per-video stats are not wired for {platform} yet — connect the channel "
    "and check the platform's own analytics for now."
)

#: Documented YouTube best practice, shown ONLY when labelled "general".
#: Sources: YouTube Creator Academy / Creator Insider guidance as of 2026 —
#: hooks in the first 2 seconds, Shorts 15–60s, consistency over volume,
#: weekday evenings + weekend middays as a starting point (own data wins
#: once there is enough of it).
GENERAL_BEST_PRACTICE: list[dict[str, str]] = [
    {
        "topic": "Hook in the first 2 seconds",
        "detail": "Say or show the payoff before anything else — viewers decide "
        "whether to stay or swipe almost immediately.",
    },
    {
        "topic": "Keep Shorts 15–60 seconds",
        "detail": "Short, complete ideas outperform long clips that get swiped "
        "away halfway through.",
    },
    {
        "topic": "Post consistently, 3–5 times a week",
        "detail": "A steady schedule beats bursts: the algorithm and your "
        "audience both learn when to expect you.",
    },
    {
        "topic": "Start with weekday evenings / weekend middays",
        "detail": "A common starting point is 18:00–21:00 on weekdays and "
        "around midday on weekends (local time). Replace this with your own "
        "best hour once you have 5+ posted clips with stats.",
    },
    {
        "topic": "Titles promise one clear thing",
        "detail": "The best-performing Shorts titles make one specific promise "
        "the first seconds of the clip deliver on.",
    },
]


class GrowthFetchError(RuntimeError):
    """A live stats fetch failed (network / auth / provider error)."""


# ------------------------------------------------------------------ pure helpers
def views_per_day(views: int, posted_at: datetime, now: datetime) -> float:
    """Views per day since posting. Minimum window is 1 hour, so a clip
    posted minutes ago does not divide by ~0 and explode."""
    posted = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=UTC)
    moment = now if now.tzinfo else now.replace(tzinfo=UTC)
    days = max((moment - posted).total_seconds() / 86400.0, 1.0 / 24.0)
    return round(views / days, 1)


def engagement_rate(likes: int, views: int) -> float | None:
    """Likes / views as a percentage. None when there are no views yet —
    0% would pretend the clip was measured and failed."""
    if views <= 0:
        return None
    return round(likes / views * 100.0, 2)


def trend_for(
    current_views: int,
    previous: dict[str, Any] | None,
    fetched_at: datetime,
) -> str:
    """Trend vs the previous snapshot. One snapshot can never show a
    trend — that clip is "new" until a second fetch exists."""
    if not previous:
        return "new"
    prev_fetched = previous.get("fetched_at")
    if prev_fetched:
        try:
            prev_dt = datetime.fromisoformat(prev_fetched)
        except ValueError:
            prev_dt = None
        if prev_dt is not None and fetched_at - prev_dt < TREND_MIN_GAP:
            return "unknown"  # too soon to tell — fetched twice within an hour
    delta = current_views - int(previous.get("views") or 0)
    if delta > 0:
        return "growing"
    if delta == 0:
        return "flat"
    return "falling"


# ------------------------------------------------------------------ posted clips
def collect_posted_clips(db: Session) -> list[dict[str, Any]]:
    """Every published post, as the posted-clip contract (see docstring).

    Posts without a provider_post_id (e.g. published before the adapter
    recorded one) are included with ``posted_video_id=None`` — the UI
    shows them as "posted, stats unavailable" rather than hiding them.
    """
    clips: list[dict[str, Any]] = []
    posts = (
        db.query(Post)
        .filter_by(status="published")
        .order_by(Post.published_at.desc(), Post.created_at.desc())
        .all()
    )
    for post in posts:
        result = dict(post.publish_result_json or {})
        seo = dict(post.seo_json or {})
        title = seo.get("title") or seo.get("caption") or seo.get("description") or ""
        niche = ""
        if post.clip_candidate_id:
            cand = db.get(ClipCandidate, post.clip_candidate_id)
            if cand is not None:
                job = db.get(Job, cand.job_id)
                if job is not None:
                    niche = str((dict(job.meta_json or {})).get("niche") or "")
        posted_at = post.published_at or post.created_at
        clips.append(
            {
                "post_id": post.id,
                "posted_video_id": result.get("provider_post_id"),
                "platform": post.platform,
                "title": title,
                "posted_at": posted_at.isoformat() if posted_at else None,
                "niche": niche,
            }
        )
    return clips


# ------------------------------------------------------------------ snapshots
def _snapshot_path() -> Path:
    return _data_dir() / SNAPSHOT_FILENAME


def load_snapshots() -> dict[str, dict[str, Any]]:
    """Previous per-video stats: {video_id: {views, likes, comments,
    fetched_at}}. Empty dict when nothing was ever fetched."""
    try:
        raw = json.loads(_snapshot_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_snapshots(snapshots: dict[str, dict[str, Any]]) -> None:
    path = _snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshots, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ YouTube stats
def youtube_auth() -> tuple[str | None, str | None, str]:
    """How a YouTube stats call would authenticate.

    Returns (access_token, api_key, status) where status is
    "oauth" | "api_key" | "not_connected". The OAuth token is read from
    the encrypted TokenStore — never from the database, never logged.
    An expired token counts as not connected: the Channels page owns
    refresh / reconnect.
    """
    try:
        record = TokenStore(SecretStore(_data_dir())).load("youtube")
    except Exception:
        record = None
    if record is not None and record.access_token and not record.is_expired():
        return record.access_token, None, "oauth"
    api_key = os.environ.get("YOUTUBE_API_KEY") or ""
    if api_key:
        return None, api_key, "api_key"
    return None, None, "not_connected"


def fetch_youtube_stats(
    video_ids: list[str],
    *,
    access_token: str | None = None,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, dict[str, int]]:
    """Fetch current statistics for up to 50 YouTube video ids.

    Returns {video_id: {"views": int, "likes": int, "comments": int}}.
    Ids the API does not return (deleted / private / wrong channel) are
    simply absent — the caller marks those clips "unavailable", it does
    not invent zeros. Raises GrowthFetchError on transport / HTTP /
    provider errors so the caller can fall back to stored snapshots.
    """
    ids = [v for v in video_ids if v][:YOUTUBE_BATCH_SIZE]
    if not ids:
        return {}
    headers: dict[str, str] = {}
    params: dict[str, str] = {"part": "statistics", "id": ",".join(ids)}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    elif api_key:
        params["key"] = api_key
    else:
        raise GrowthFetchError("YouTube stats need a connected channel or a YOUTUBE_API_KEY.")
    own_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        resp = client.get(YOUTUBE_STATS_URL, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise GrowthFetchError(f"YouTube stats request failed: {exc}") from exc
    finally:
        if own_client:
            client.close()
    if resp.status_code != 200:
        raise GrowthFetchError(f"YouTube API returned HTTP {resp.status_code} for videos.list")
    try:
        items = resp.json().get("items", [])
    except ValueError as exc:
        raise GrowthFetchError("YouTube API returned an unreadable response") from exc
    out: dict[str, dict[str, int]] = {}
    for item in items:
        stats = item.get("statistics", {}) or {}
        try:
            out[str(item.get("id"))] = {
                "views": int(stats.get("viewCount") or 0),
                "likes": int(stats.get("likeCount") or 0),
                "comments": int(stats.get("commentCount") or 0),
            }
        except (TypeError, ValueError):
            continue
    return out


def fetch_all_stats(
    clips: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, int]], dict[str, str]]:
    """Fetch stats for every posted clip that can have them.

    Returns (stats_by_video_id, status_by_platform). YouTube ids are
    fetched in one batched call; other platforms report "unsupported";
    YouTube with no credential reports "not_connected". A failed live
    fetch raises GrowthFetchError — the caller falls back to snapshots.
    """
    yt_ids = [
        c["posted_video_id"]
        for c in clips
        if c["platform"] == "youtube" and c.get("posted_video_id")
    ]
    status: dict[str, str] = {}
    stats: dict[str, dict[str, int]] = {}
    platforms = {c["platform"] for c in clips}
    for platform in platforms:
        if platform not in SUPPORTED_STATS_PLATFORMS:
            status[platform] = "unsupported"
    if "youtube" in platforms:
        if not yt_ids:
            status["youtube"] = "no_video_id"
        else:
            access_token, api_key, auth_status = youtube_auth()
            if auth_status == "not_connected":
                status["youtube"] = "not_connected"
            else:
                stats = fetch_youtube_stats(yt_ids, access_token=access_token, api_key=api_key)
                status["youtube"] = "live"
    return stats, status


# ------------------------------------------------------------------ enrichment
def enrich_clips(
    clips: list[dict[str, Any]],
    stats: dict[str, dict[str, int]],
    snapshots: dict[str, dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """Attach stats to posted clips. A clip with no live stats falls back
    to its stored snapshot (marked ``stale: True``); a clip with neither
    shows ``stats_status`` and null numbers — never zeros."""
    rows: list[dict[str, Any]] = []
    for clip in clips:
        video_id = clip.get("posted_video_id")
        platform = clip["platform"]
        live = stats.get(video_id) if video_id else None
        previous = snapshots.get(video_id) if video_id else None
        if live is not None:
            source: dict[str, Any] = dict(live)
            stale = False
        elif previous is not None:
            source = {
                "views": int(previous.get("views") or 0),
                "likes": int(previous.get("likes") or 0),
                "comments": int(previous.get("comments") or 0),
            }
            stale = True
        else:
            source = {"views": None, "likes": None, "comments": None}
            stale = False
        posted_at = clip.get("posted_at")
        try:
            posted_dt = datetime.fromisoformat(posted_at) if posted_at else None
        except ValueError:
            posted_dt = None
        vpd = (
            views_per_day(source["views"], posted_dt, now)
            if source["views"] is not None and posted_dt is not None
            else None
        )
        if platform not in SUPPORTED_STATS_PLATFORMS:
            stats_status = "unsupported"
        elif not video_id:
            stats_status = "no_video_id"
        elif live is not None:
            stats_status = "live"
        elif previous is not None:
            stats_status = "stale"
        else:
            stats_status = "unavailable"
        trend = (
            trend_for(source["views"], previous, now) if source["views"] is not None else "unknown"
        )
        rows.append(
            {
                **clip,
                "views": source["views"],
                "likes": source["likes"],
                "comments": source["comments"],
                "views_per_day": vpd,
                "engagement_pct": (
                    engagement_rate(source["likes"], source["views"])
                    if source["views"] is not None
                    else None
                ),
                "trend": trend,
                "stale": stale,
                "stats_status": stats_status,
            }
        )
    return rows


# ------------------------------------------------------------------ advice
def _basis(own_n: int) -> dict[str, Any]:
    """The honesty label every advice section carries."""
    if own_n >= MIN_CLIPS_FOR_ADVICE:
        return {"kind": "own_data", "label": f"from your {own_n} posted clips", "n": own_n}
    return {
        "kind": "general",
        "label": (
            "YouTube best practice (general) — not your data yet: "
            f"you have {own_n} posted clip(s) with stats, "
            f"{MIN_CLIPS_FOR_ADVICE} needed for advice from your own numbers"
        ),
        "n": own_n,
    }


def compute_advice(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Turn measured clips into advice. Rules (all deterministic):

    - Only clips WITH stats (views is not None) count toward own-data
      advice. A clip posted yesterday with no fetch yet proves nothing.
    - Best posting times: mean views/day per posted hour (UTC). Needs
      >= MIN_CLIPS_FOR_ADVICE clips with stats AND >= 2 distinct hours;
      otherwise the general starting point is shown instead.
    - Best titles: top 3 clips by views/day (title shown verbatim so the
      user can copy the pattern).
    - Niches: mean views/day per niche (clips with no niche are skipped,
      not lumped into a fake "general" bucket).
    - What to post next: built from the top niche + top title when own
      data qualifies, else general guidance.
    """
    measured = [r for r in rows if r.get("views") is not None]
    n = len(measured)
    basis = _basis(n)
    general_basis = {
        "kind": "general",
        "label": (
            "YouTube best practice (general) — not your data yet: "
            f"you have {n} posted clip(s) with stats, "
            f"{MIN_CLIPS_FOR_ADVICE} needed for advice from your own numbers"
        ),
        "n": n,
    }

    # --- best posting times (hour of day, UTC) ---
    by_hour: dict[int, list[float]] = {}
    for row in measured:
        try:
            hour = datetime.fromisoformat(row["posted_at"]).hour
        except (TypeError, ValueError):
            continue
        by_hour.setdefault(hour, []).append(row["views_per_day"] or 0.0)
    hour_means = sorted(
        ((hour, round(sum(v) / len(v), 1)) for hour, v in by_hour.items()),
        key=lambda kv: kv[1],
        reverse=True,
    )
    if n >= MIN_CLIPS_FOR_ADVICE and len(hour_means) >= 2:
        best_times = {
            "basis": basis,
            "best_hours_utc": [h for h, _ in hour_means[:3]],
            "hours": [{"hour_utc": h, "avg_views_per_day": m} for h, m in hour_means[:3]],
            "note": (
                "Hours are UTC — your posted clips' own timestamps vs their "
                "views per day. Post more clips at these hours and re-check."
            ),
        }
    else:
        best_times = {
            "basis": general_basis,
            "best_hours_utc": [18, 19, 20],
            "hours": [],
            "note": (
                "General starting point (weekday evenings, UTC): post at "
                "18:00–21:00 UTC and weekend middays until your own clips "
                "show a better hour."
            ),
        }

    # --- best titles / hooks ---
    # This is a factual listing of the user's own clips, not a
    # recommendation, so it is labelled with the real clip count even
    # below the threshold — "from your 2 posted clips" is honest.
    ranked = sorted(measured, key=lambda r: r.get("views_per_day") or 0.0, reverse=True)
    titles_basis = (
        {"kind": "own_data", "label": f"from your {n} posted clip(s) with stats", "n": n}
        if n >= 1
        else general_basis
    )
    best_titles = {
        "basis": titles_basis,
        "top": [
            {
                "post_id": r["post_id"],
                "title": r["title"],
                "platform": r["platform"],
                "views": r["views"],
                "views_per_day": r["views_per_day"],
            }
            for r in ranked[:3]
        ],
    }

    # --- niches / topics ---
    # Ranking niches from a handful of clips would pretend a pattern
    # exists, so below the threshold no ranking is shown at all.
    by_niche: dict[str, list[float]] = {}
    for row in measured:
        niche = (row.get("niche") or "").strip()
        if not niche:
            continue
        by_niche.setdefault(niche, []).append(row["views_per_day"] or 0.0)
    if n >= MIN_CLIPS_FOR_ADVICE:
        niche_means = sorted(
            (
                {
                    "niche": niche,
                    "clips": len(v),
                    "avg_views_per_day": round(sum(v) / len(v), 1),
                }
                for niche, v in by_niche.items()
            ),
            key=lambda d: d["avg_views_per_day"],
            reverse=True,
        )
        niches = {"basis": basis, "ranking": niche_means}
    else:
        niche_means = []
        niches = {"basis": general_basis, "ranking": []}

    # --- what to post next ---
    suggestions: list[dict[str, str]] = []
    if n >= MIN_CLIPS_FOR_ADVICE and niche_means:
        top_niche = niche_means[0]["niche"]
        top_title = ranked[0]["title"] if ranked else ""
        suggestions.append(
            {
                "text": f"More {top_niche} clips — it averages "
                f"{niche_means[0]['avg_views_per_day']} views/day, your best niche.",
                "basis": basis["label"],
            }
        )
        if top_title:
            suggestions.append(
                {
                    "text": f"Reuse the pattern of your best title: “{top_title}”.",
                    "basis": basis["label"],
                }
            )
        if best_times["best_hours_utc"]:
            suggestions.append(
                {
                    "text": f"Post the next clip around "
                    f"{best_times['best_hours_utc'][0]:02d}:00 UTC, your best hour so far.",
                    "basis": basis["label"],
                }
            )
    else:
        suggestions.append(
            {
                "text": "Post your first clips in the niche your channel is about, "
                "then come back here — after 5 posted clips this page can tell "
                "you what actually works for you.",
                "basis": general_basis["label"],
            }
        )
        suggestions.append(
            {
                "text": "Until then: hook in the first 2 seconds, keep Shorts "
                "15–60s, and post 3–5 times a week.",
                "basis": "YouTube best practice (general)",
            }
        )

    return {
        "best_posting_times": best_times,
        "best_titles": best_titles,
        "niches": niches,
        "what_to_post_next": suggestions,
        "general_best_practice": GENERAL_BEST_PRACTICE,
        "min_clips_for_own_data": MIN_CLIPS_FOR_ADVICE,
    }


# ------------------------------------------------------------------ endpoints
def _build_view(db: Session, *, refresh: bool) -> dict[str, Any]:
    """Shared by GET /growth and POST /growth/refresh.

    ``refresh=True`` re-fetches live stats and stores a snapshot.
    ``refresh=False`` still tries a live fetch (cheap: 1 quota unit per
    50 clips) but falls back to stored snapshots on any failure, so the
    page works offline and never blocks on the network.
    """
    now = datetime.now(UTC)
    clips = collect_posted_clips(db)
    snapshots = load_snapshots()
    stats: dict[str, dict[str, int]] = {}
    platform_status: dict[str, str] = {}
    fetch_error: str | None = None
    if clips:
        try:
            stats, platform_status = fetch_all_stats(clips)
        except GrowthFetchError as exc:
            fetch_error = str(exc)
            platform_status = {
                c["platform"]: ("stale" if c.get("posted_video_id") else "unavailable")
                for c in clips
            }
    if refresh and stats:
        merged = dict(snapshots)
        for video_id, values in stats.items():
            merged[video_id] = {**values, "fetched_at": now.isoformat()}
        save_snapshots(merged)
        snapshots = merged
    rows = enrich_clips(clips, stats, snapshots, now)
    measured = [r for r in rows if r.get("views") is not None]
    totals = {
        "posted_clips": len(rows),
        "clips_with_stats": len(measured),
        "total_views": sum(r["views"] for r in measured),
        "total_likes": sum(r["likes"] for r in measured),
        "total_comments": sum(r["comments"] for r in measured),
        "avg_views_per_day": (
            round(sum(r["views_per_day"] or 0.0 for r in measured) / len(measured), 1)
            if measured
            else None
        ),
    }
    return {
        "posted_clips": rows,
        "totals": totals,
        "advice": compute_advice(rows, now),
        "platform_status": platform_status,
        "fetch_error": fetch_error,
        "fetched_at": now.isoformat(),
        "has_posted_clips": len(rows) > 0,
    }


@router.get("")
def get_growth(db: Session = Depends(get_db)):
    """Full Growth view (live fetch when possible, snapshots otherwise)."""
    return _build_view(db, refresh=False)


@router.post("/refresh")
def refresh_growth(db: Session = Depends(get_db)):
    """Re-fetch stats now and store a snapshot (drives the trend column)."""
    return _build_view(db, refresh=True)


@router.get("/advice")
def get_growth_advice(db: Session = Depends(get_db)):
    """Advice only, from stored snapshots — no live fetch."""
    now = datetime.now(UTC)
    clips = collect_posted_clips(db)
    snapshots = load_snapshots()
    rows = enrich_clips(clips, {}, snapshots, now)
    return compute_advice(rows, now)


__all__ = [
    "router",
    "MIN_CLIPS_FOR_ADVICE",
    "GENERAL_BEST_PRACTICE",
    "GrowthFetchError",
    "views_per_day",
    "engagement_rate",
    "trend_for",
    "collect_posted_clips",
    "load_snapshots",
    "save_snapshots",
    "youtube_auth",
    "fetch_youtube_stats",
    "fetch_all_stats",
    "enrich_clips",
    "compute_advice",
]
