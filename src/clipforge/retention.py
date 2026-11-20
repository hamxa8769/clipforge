"""Retention analytics → next-clip feedback for ClipForge (Phase 2).

Two parts:

1. **Ingestion** — :func:`store_retention_point` validates and stores
   one retention observation (avg view duration, completion %,
   viewed-vs-swiped %, drop-off second, views) for a post/clip. The
   numbers arrive manually or from a platform export. **There is no
   live platform feed in this release**: the YouTube Analytics /
   Meta Insights / TikTok Display ingest stubs in
   :mod:`clipforge.analytics` still return ``not_configured`` without
   credentials, and no key exists. The data model and the feedback
   loop below work on whatever points are stored, however they got
   there — so the loop is testable today with synthetic data and
   becomes live automatically once a feed is wired.
2. **Feedback** — :func:`feedback_from_points` aggregates stored
   points into adjustments, and :func:`adjust_candidate_scores`
   applies them to the next batch of candidates: hooks that dropped
   viewers off in the first 3 s are penalised, formats (signals) that
   retained viewers are boosted, and clips are shortened when
   completion is low. All rules are transparent and bounded — the
   adjustments are reported alongside the scores, never silently
   baked in.

Thresholds follow the research report (§4): completion >70% is good
for <30 s clips; viewed% (viewed-vs-swiped) is the Shorts feed's main
signal; a drop-off in the first 3 s means the hook failed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from sqlalchemy.orm import Session

from clipforge.db import Post, RetentionPoint

PLATFORMS = ("youtube", "instagram", "facebook", "tiktok")
SOURCES = ("manual", "youtube", "meta", "tiktok", "export")

# Feedback bounds — adjustments never dominate the base score.
MAX_HOOK_PENALTY = 15.0  # points off a candidate whose hook pattern failed
MAX_SIGNAL_BOOST = 10.0  # points on for a signal that retained viewers
EARLY_DROPOFF_S = 3.0  # drop-off at/before this = hook failure
GOOD_COMPLETION_PCT = 70.0
POOR_COMPLETION_PCT = 50.0
GOOD_VIEWED_PCT = 70.0
POOR_VIEWED_PCT = 50.0


class RetentionError(ValueError):
    """Raised when a retention point fails validation."""


@dataclass
class RetentionInput:
    """One retention observation, as supplied to the ingest endpoint."""

    platform: str
    clip_duration_s: float
    avg_view_duration_s: float | None = None
    completion_pct: float | None = None
    viewed_pct: float | None = None
    dropoff_second: float | None = None
    views: int = 0
    post_id: str | None = None
    clip_candidate_id: str | None = None
    source: str = "manual"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RetentionFeedback:
    """Aggregated feedback from a set of retention points."""

    points_used: int = 0
    avg_completion_pct: float | None = None
    avg_viewed_pct: float | None = None
    early_dropoff_rate: float | None = None  # fraction of points with drop-off <= 3 s
    hook_penalty: float = 0.0  # points to subtract from hook-heavy candidates
    signal_boosts: dict[str, float] = field(default_factory=dict)
    recommended_max_duration_s: float | None = None
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def validate_retention(data: RetentionInput, session: Session | None = None) -> RetentionInput:
    """Validate one retention observation; raises :class:`RetentionError`."""
    if data.platform not in PLATFORMS:
        raise RetentionError(f"platform must be one of {PLATFORMS}, got {data.platform!r}")
    if data.clip_duration_s <= 0:
        raise RetentionError("clip_duration_s must be > 0")
    if data.source not in SOURCES:
        raise RetentionError(f"source must be one of {SOURCES}, got {data.source!r}")
    if data.views < 0:
        raise RetentionError("views must be >= 0")
    for name in ("completion_pct", "viewed_pct"):
        value = getattr(data, name)
        if value is not None and not 0 <= value <= 100:
            raise RetentionError(f"{name} must be 0–100, got {value!r}")
    if data.avg_view_duration_s is not None and data.avg_view_duration_s < 0:
        raise RetentionError("avg_view_duration_s must be >= 0")
    if data.dropoff_second is not None:
        if data.dropoff_second < 0:
            raise RetentionError("dropoff_second must be >= 0")
        if data.dropoff_second > data.clip_duration_s:
            raise RetentionError(
                f"dropoff_second {data.dropoff_second}s exceeds clip duration "
                f"{data.clip_duration_s}s"
            )
    if session is not None:
        if data.post_id and session.get(Post, data.post_id) is None:
            raise RetentionError(f"post {data.post_id!r} not found")
    return data


def store_retention_point(session: Session, data: RetentionInput) -> RetentionPoint:
    """Validate + store one retention point. Returns the stored row."""
    validate_retention(data, session)
    point = RetentionPoint(
        post_id=data.post_id,
        clip_candidate_id=data.clip_candidate_id,
        platform=data.platform,
        clip_duration_s=data.clip_duration_s,
        avg_view_duration_s=data.avg_view_duration_s,
        completion_pct=data.completion_pct,
        viewed_pct=data.viewed_pct,
        dropoff_second=data.dropoff_second,
        views=data.views,
        source=data.source,
    )
    session.add(point)
    session.commit()
    return point


def _mean(values: list[float | None]) -> float | None:
    nums = [v for v in values if v is not None]
    return sum(nums) / len(nums) if nums else None


def feedback_from_points(points: list[RetentionPoint | RetentionInput]) -> RetentionFeedback:
    """Aggregate retention points into next-clip feedback.

    Rules (all transparent, all bounded):
    * Early drop-off (``dropoff_second <= 3``) in >= half the points
      → hook penalty up to 15 pts, scaled by the early-drop-off rate.
    * Mean completion < 50% → recommend shortening: the next clips'
      target duration drops to the mean viewed duration (avg view
      duration across points), floored at 15 s.
    * Mean completion >= 70% and viewed% >= 70% → no penalty; the
      format is working, so the recommendation is to reuse it.
    * ``signal_boosts`` is populated by
      :func:`feedback_with_signals` when per-clip signal data is
      available; the plain aggregate leaves it empty rather than
      inventing a boost.
    """
    feedback = RetentionFeedback(points_used=len(points))
    if not points:
        feedback.recommendations.append(
            "No retention data yet — publish, then re-check viewed% / "
            "completion before cutting the next clip."
        )
        return feedback
    feedback.avg_completion_pct = _mean([p.completion_pct for p in points])
    feedback.avg_viewed_pct = _mean([p.viewed_pct for p in points])
    dropoffs = [p.dropoff_second for p in points if p.dropoff_second is not None]
    if dropoffs:
        early = sum(1 for d in dropoffs if d <= EARLY_DROPOFF_S)
        feedback.early_dropoff_rate = early / len(dropoffs)
        if feedback.early_dropoff_rate >= 0.5:
            feedback.hook_penalty = round(MAX_HOOK_PENALTY * feedback.early_dropoff_rate, 1)
            feedback.recommendations.append(
                f"Hook failing: {early}/{len(dropoffs)} clips dropped off within "
                f"{EARLY_DROPOFF_S:.0f}s — put the payoff/keyword in the first "
                "1–3s and re-cut the opening."
            )
    avg_completion = feedback.avg_completion_pct
    if avg_completion is not None and avg_completion < POOR_COMPLETION_PCT:
        avg_viewed = _mean([p.avg_view_duration_s for p in points])
        if avg_viewed:
            feedback.recommended_max_duration_s = round(max(15.0, avg_viewed), 1)
            feedback.recommendations.append(
                f"Low completion ({feedback.avg_completion_pct:.0f}%): shorten next "
                f"clips towards ~{feedback.recommended_max_duration_s:.0f}s "
                "(mean viewed duration) and move the payoff earlier."
            )
    if (
        feedback.avg_completion_pct is not None
        and feedback.avg_completion_pct >= GOOD_COMPLETION_PCT
        and feedback.avg_viewed_pct is not None
        and feedback.avg_viewed_pct >= GOOD_VIEWED_PCT
    ):
        feedback.recommendations.append(
            "Strong retention (completion and viewed% both >=70%): "
            "this hook/format works — reuse its pattern."
        )
    if not feedback.recommendations:
        feedback.recommendations.append(
            "Mixed retention — no strong signal yet; keep clip length near "
            "30s and watch the next batch before changing the formula."
        )
    return feedback


def feedback_with_signals(
    points: list[dict],
) -> RetentionFeedback:
    """Feedback that also boosts the signals of clips that retained viewers.

    ``points`` are dicts with retention fields plus ``signals`` (the
    per-signal scores of the clip, as in
    :class:`clipforge.highlights.Candidate.signals`) and optionally
    ``hook`` (1–10). A clip counts as "retained" when completion >=70%;
    each of its signals above 0.5 earns a boost proportional to its
    strength, capped at :data:`MAX_SIGNAL_BOOST` in total per signal.
    """
    fields = RetentionInput.__dataclass_fields__
    rows = [RetentionInput(**{k: v for k, v in p.items() if k in fields}) for p in points]
    feedback = feedback_from_points(rows)
    boosts: dict[str, float] = {}
    for p in points:
        completion = p.get("completion_pct")
        signals = p.get("signals") or {}
        if completion is None or completion < GOOD_COMPLETION_PCT:
            continue
        for name, strength in signals.items():
            if strength and strength >= 0.5:
                boosts[name] = boosts.get(name, 0.0) + round(MAX_SIGNAL_BOOST * 0.25 * strength, 1)
    feedback.signal_boosts = {k: min(v, MAX_SIGNAL_BOOST) for k, v in boosts.items()}
    if feedback.signal_boosts:
        best = max(feedback.signal_boosts, key=feedback.signal_boosts.get)
        feedback.recommendations.append(
            f"Formats that retained viewers score on '{best}' — "
            "boost similar moments in the next batch."
        )
    return feedback


def adjust_candidate_scores(candidates: list[dict], feedback: RetentionFeedback) -> list[dict]:
    """Apply retention feedback to next-batch candidate scores.

    Each candidate is a dict with ``score`` (0–100), ``hook`` (1–10)
    and ``signals`` (name → 0–1). Returns new dicts with
    ``adjusted_score`` (clamped 0–100), ``adjustment`` (signed points)
    and ``adjustment_reason``. The base ``score`` is never mutated —
    the adjustment is reported next to it so the review UI can show
    both.
    """
    out: list[dict] = []
    for cand in candidates:
        score = float(cand.get("score", 0.0))
        adjustment = 0.0
        reasons: list[str] = []
        # Penalise hook-heavy candidates when hooks are failing.
        if feedback.hook_penalty:
            hook = float(cand.get("hook", 5))
            penalty = feedback.hook_penalty * (hook / 10.0)
            adjustment -= penalty
            reasons.append(f"hook penalty −{penalty:.1f} (early drop-off in past clips)")
        # Boost signals that retained viewers.
        for name, boost in feedback.signal_boosts.items():
            strength = float((cand.get("signals") or {}).get(name, 0.0))
            if strength >= 0.5:
                gain = boost * strength
                adjustment += gain
                reasons.append(f"signal '{name}' boost +{gain:.1f} (retained viewers before)")
        # Shorten preference: when completion is poor, long clips lose points.
        if feedback.recommended_max_duration_s is not None:
            duration = float(
                cand.get("duration_s") or cand.get("end_s", 0) - cand.get("start_s", 0)
            )
            cap = feedback.recommended_max_duration_s
            if duration > cap:
                over = min(10.0, (duration - cap) * 0.5)
                adjustment -= over
                reasons.append(f"length penalty −{over:.1f} (>{cap:.0f}s target from retention)")
        adjusted = max(0.0, min(100.0, score + adjustment))
        out.append(
            {
                **cand,
                "adjusted_score": round(adjusted, 1),
                "adjustment": round(adjustment, 1),
                "adjustment_reason": "; ".join(reasons) if reasons else "no retention adjustment",
            }
        )
    return sorted(out, key=lambda c: c["adjusted_score"], reverse=True)


def point_to_dict(point: RetentionPoint) -> dict:
    return {
        "id": point.id,
        "post_id": point.post_id,
        "clip_candidate_id": point.clip_candidate_id,
        "platform": point.platform,
        "clip_duration_s": point.clip_duration_s,
        "avg_view_duration_s": point.avg_view_duration_s,
        "completion_pct": point.completion_pct,
        "viewed_pct": point.viewed_pct,
        "dropoff_second": point.dropoff_second,
        "views": point.views,
        "source": point.source,
        "created_at": point.created_at.isoformat() if point.created_at else None,
    }


__all__ = [
    "RetentionInput",
    "RetentionFeedback",
    "RetentionError",
    "PLATFORMS",
    "SOURCES",
    "validate_retention",
    "store_retention_point",
    "feedback_from_points",
    "feedback_with_signals",
    "adjust_candidate_scores",
    "point_to_dict",
]
