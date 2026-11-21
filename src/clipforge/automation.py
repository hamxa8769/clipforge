"""Automation rules engine for ClipForge v0.4.0.

Three rules, all toggleable, all logged — nothing automated is silent:

* ``auto_seo_on_approve`` — when a clip candidate is approved in
  review, generate + store its per-platform SEO variants
  (:class:`CandidateSeo`) so scheduling never has to re-generate copy.
* ``auto_pick_next_clip`` — pick the next clip to cut from retention
  feedback: early drop-off (≤3 s) penalises hook-heavy candidates,
  formats that retained viewers get a signal boost, poor completion
  shortens the target length (see :mod:`clipforge.retention`). The pick
  is a *recommendation* — it never approves anything by itself.
* ``auto_queue_approved`` — fan-out compose (the
  freespirits/social-auto-engine pattern, see docs/REUSE.md): one
  approved clip becomes one ``pending_approval`` post per connected
  platform, respecting the per-platform daily caps
  (:mod:`clipforge.publishing.limits`). Queued posts still need a
  human to approve the *post* before anything can publish, and a
  platform whose *channel* is not connected is skipped (logged, not
  faked). "Connected" is the canonical ``GET /channels`` status
  (:mod:`clipforge.auth.channels` — OAuth token or legacy env token);
  a bare ``SocialAccount`` row (``GET /accounts``, legacy) does not
  connect a channel, it only links the post and carries the cap
  override.

Rules live in the DB (:class:`AutomationRule`), seeded from
:func:`DEFAULT_RULES` on first use. Every firing — and every skip
(cap reached, no account, rule disabled, already queued) — writes an
:class:`AutomationEvent` row, which ``GET /automation/feed`` serves.
Dry-run stays the default: automation only ever creates
``pending_approval`` posts; publishing still needs human approval and
``dry_run=false`` + credentials.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from clipforge.auth.channels import channel_status
from clipforge.auth.secrets import SecretStore
from clipforge.db import (
    AutomationEvent,
    AutomationRule,
    CandidateSeo,
    ClipCandidate,
    Job,
    JobStatus,
    Post,
    PostStatus,
    RetentionPoint,
    SocialAccount,
)
from clipforge.publishing.limits import DAILY_CAPS
from clipforge.retention import adjust_candidate_scores, feedback_from_points
from clipforge.scheduler import published_today_count
from clipforge.seo import generate_all as generate_all_seo
from clipforge.workflow import MIN_SCORE_THRESHOLD, score_band

PLATFORMS = ("youtube", "instagram", "facebook", "tiktok")

# Best-window hours (UTC) per platform, from the research report §4:
# Reels 11:00, Facebook 13:00, Shorts 15:00, TikTok 19:00. The
# staggered_schedule rule places each auto-queued post in its own
# window instead of blasting all four at once.
DEFAULT_WINDOWS = {"instagram": 11, "facebook": 13, "youtube": 15, "tiktok": 19}

DEFAULT_RULES: list[dict] = [
    {
        "name": "Auto-generate SEO on clip approval",
        "kind": "auto_seo_on_approve",
        "enabled": True,
        "config": {"platforms": list(PLATFORMS)},
    },
    {
        "name": "Auto-pick next clip from retention feedback",
        "kind": "auto_pick_next_clip",
        "enabled": True,
        "config": {"min_score": MIN_SCORE_THRESHOLD},
    },
    {
        "name": "Auto-queue approved clips per platform caps",
        "kind": "auto_queue_approved",
        "enabled": True,
        "config": {"platforms": list(PLATFORMS)},
    },
    {
        "name": "Staggered scheduling",
        "kind": "staggered_schedule",
        "enabled": True,
        "config": {"windows_utc": dict(DEFAULT_WINDOWS)},
    },
    {
        "name": "Auto-approve very high scores",
        "kind": "auto_approve_high_score",
        "enabled": False,
        "config": {"min_score": 95.0, "min_hook": 9},
    },
]

# Frontend rule keys (docs/API-CONTRACT.md §1, apps/web/app/automation)
# → backend rule kinds. The automation page's toggles use these keys.
FRONTEND_KEYS = {
    "retention_feedback": "auto_pick_next_clip",
    "auto_seo": "auto_seo_on_approve",
    "staggered_schedule": "staggered_schedule",
    "auto_approve_high_score": "auto_approve_high_score",
}
BACKEND_KEYS = {v: k for k, v in FRONTEND_KEYS.items()}

# Label + description per rule, in the frontend's wording
# (docs/API-CONTRACT.md §1) so GET /automation/rules can serve the
# Automation page directly.
RULE_DESCRIPTIONS = {
    "auto_pick_next_clip": (
        "Retention-feedback clip picking",
        "Prefer hooks and lengths that scored well on viewed% / completion "
        "in the last analytics snapshot; down-rank patterns that failed.",
    ),
    "auto_seo_on_approve": (
        "Auto-SEO variants",
        "Generate per-platform titles, descriptions and hashtags for every "
        "approved clip (YouTube / TikTok / Instagram / Facebook rules).",
    ),
    "auto_queue_approved": (
        "Auto-queue approved clips",
        "Fan-out one approved clip to one pending-approval post per "
        "connected platform, respecting the per-platform daily caps.",
    ),
    "staggered_schedule": (
        "Staggered scheduling",
        "Place each platform's post in its own best window (IG 11:00, "
        "FB 13:00, YT 15:00, TikTok 19:00 UTC) instead of blasting all "
        "four at once.",
    ),
    "auto_approve_high_score": (
        "Auto-approve very high scores",
        "Off by default and intentionally conservative: only clips scoring "
        "95+ with a hook rating of 9+ skip the human review queue. "
        "Post-level approval before publishing still applies.",
    ),
}


def resolve_kind(key: str) -> str | None:
    """A frontend rule key or a backend kind → backend kind, or None."""
    if key in FRONTEND_KEYS:
        return FRONTEND_KEYS[key]
    if any(spec["kind"] == key for spec in DEFAULT_RULES):
        return key
    return None


def ensure_default_rules(session: Session) -> list[AutomationRule]:
    """Seed the default rules if missing. Returns all rules."""
    for spec in DEFAULT_RULES:
        existing = session.query(AutomationRule).filter_by(kind=spec["kind"]).first()
        if existing is None:
            session.add(
                AutomationRule(
                    name=spec["name"],
                    kind=spec["kind"],
                    enabled=spec["enabled"],
                    config_json=spec["config"],
                )
            )
    session.commit()
    return session.query(AutomationRule).order_by(AutomationRule.kind.asc()).all()


def get_rule(session: Session, kind: str) -> AutomationRule | None:
    """One rule by kind. Seeds the defaults on first use, so every
    caller (API, worker, automation pass) sees the rules even when no
    endpoint ever listed them — a rule must never be ``None`` just
    because nobody called ``GET /automation/rules`` first."""
    rule = session.query(AutomationRule).filter_by(kind=kind).first()
    if rule is None and any(spec["kind"] == kind for spec in DEFAULT_RULES):
        ensure_default_rules(session)
        rule = session.query(AutomationRule).filter_by(kind=kind).first()
    return rule


def rule_enabled(session: Session, kind: str) -> bool:
    rule = get_rule(session, kind)
    if rule is None:
        return any(spec["kind"] == kind and spec["enabled"] for spec in DEFAULT_RULES)
    return bool(rule.enabled)


def log_event(
    session: Session,
    kind: str,
    action: str,
    detail: dict | None = None,
    job_id: str | None = None,
    clip_candidate_id: str | None = None,
    post_id: str | None = None,
    actor: str = "automation",
) -> AutomationEvent:
    event = AutomationEvent(
        kind=kind,
        action=action,
        actor=actor,
        job_id=job_id,
        clip_candidate_id=clip_candidate_id,
        post_id=post_id,
        detail_json=detail or {},
    )
    session.add(event)
    session.commit()
    return event


def rule_out(rule: AutomationRule) -> dict:
    """Contract shape (docs/API-CONTRACT.md §1): ``key``/``label``/
    ``enabled``/``description`` for the Automation page, plus the
    backend ``kind``, ``id`` and ``config`` for the API."""
    label, description = RULE_DESCRIPTIONS.get(rule.kind, (rule.name, ""))
    return {
        "id": rule.id,
        "key": BACKEND_KEYS.get(rule.kind, rule.kind),
        "kind": rule.kind,
        "label": label,
        "name": rule.name,
        "enabled": rule.enabled,
        "description": description,
        "config": rule.config_json or {},
        "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
    }


def event_out(event: AutomationEvent) -> dict:
    return {
        "id": event.id,
        "kind": event.kind,
        "action": event.action,
        "actor": event.actor,
        "job_id": event.job_id,
        "clip_candidate_id": event.clip_candidate_id,
        "post_id": event.post_id,
        "detail": event.detail_json or {},
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


# ------------------------------------------------------------------ SEO
def _seo_keyword_for(session: Session, candidate: ClipCandidate) -> str:
    job = session.get(Job, candidate.job_id)
    return ((job.title if job else "") or "").split(":")[0].strip() or "clip"


def generate_seo_for_candidate(
    session: Session,
    candidate: ClipCandidate,
    platforms: list[str] | None = None,
    generated_by: str = "automation",
) -> dict[str, dict]:
    """Generate + store SEO variants for a candidate. Returns platform → seo."""
    platforms = platforms or list(PLATFORMS)
    keyword = _seo_keyword_for(session, candidate)
    variants = generate_all_seo(
        keyword,
        candidate.hook_line or "",
        candidate.hook_line or "",
        candidate.duration_s or 30.0,
    )
    stored: dict[str, dict] = {}
    for platform in platforms:
        variant = variants[platform]
        existing = (
            session.query(CandidateSeo)
            .filter_by(clip_candidate_id=candidate.id, platform=platform)
            .first()
        )
        if existing:
            existing.seo_json = variant
            existing.generated_by = generated_by
        else:
            session.add(
                CandidateSeo(
                    clip_candidate_id=candidate.id,
                    platform=platform,
                    seo_json=variant,
                    generated_by=generated_by,
                )
            )
        stored[platform] = variant
    session.commit()
    return stored


def seo_for_candidate(session: Session, candidate: ClipCandidate) -> dict[str, dict]:
    """Stored SEO variants for a candidate (empty when never generated)."""
    rows = session.query(CandidateSeo).filter_by(clip_candidate_id=candidate.id).all()
    return {row.platform: row.seo_json or {} for row in rows}


# ------------------------------------------------------------- pick next
def pick_next_clip(session: Session, job_id: str) -> dict | None:
    """Recommend the next clip for a job from retention feedback.

    Pending candidates are re-scored with the same feedback the worker
    applies at score time; the top one (above ``min_score``) is the
    pick. Returns None when there is nothing pending or nothing
    qualifies — and logs the outcome either way.
    """
    job = session.get(Job, job_id)
    if job is None:
        raise ValueError(f"job {job_id} not found")
    rule = get_rule(session, "auto_pick_next_clip")
    config = (rule.config_json or {}) if rule else {}
    min_score = float(config.get("min_score", MIN_SCORE_THRESHOLD))
    pending = (
        session.query(ClipCandidate)
        .filter_by(job_id=job_id, status="pending")
        .order_by(ClipCandidate.n.asc())
        .all()
    )
    if not pending:
        log_event(
            session,
            "auto_pick_next_clip",
            "pick_skipped_no_pending",
            {"job_id": job_id, "reason": "no pending candidates"},
            job_id=job_id,
        )
        return None
    feedback = feedback_from_points(
        session.query(RetentionPoint).order_by(RetentionPoint.created_at.desc()).limit(200).all()
    )
    adjusted = adjust_candidate_scores(
        [
            {
                "id": c.id,
                "n": c.n,
                "score": c.score,
                "hook": c.hook_rating or 5,
                "signals": {},
                "duration_s": c.duration_s,
                "start_s": c.start_s,
                "end_s": c.end_s,
                "hook_line": c.hook_line,
            }
            for c in pending
        ],
        feedback,
    )
    top = adjusted[0] if adjusted else None
    if top is None or top["adjusted_score"] < min_score:
        log_event(
            session,
            "auto_pick_next_clip",
            "pick_skipped_below_threshold",
            {
                "job_id": job_id,
                "min_score": min_score,
                "best_adjusted_score": top["adjusted_score"] if top else None,
                "feedback": feedback.to_dict(),
            },
            job_id=job_id,
        )
        return None
    log_event(
        session,
        "auto_pick_next_clip",
        "picked_next_clip",
        {
            "job_id": job_id,
            "candidate_id": top.get("id"),
            "n": top.get("n"),
            "adjusted_score": top["adjusted_score"],
            "score_band": score_band(top["adjusted_score"]),
            "adjustment": top["adjustment"],
            "adjustment_reason": top["adjustment_reason"],
            "feedback": feedback.to_dict(),
        },
        job_id=job_id,
        clip_candidate_id=top.get("id"),
    )
    return {
        "candidate_id": top.get("id"),
        "n": top.get("n"),
        "job_id": job_id,
        "score": top.get("score"),
        "adjusted_score": top["adjusted_score"],
        "score_band": score_band(top["adjusted_score"]),
        "adjustment": top["adjustment"],
        "adjustment_reason": top["adjustment_reason"],
        "hook_line": top.get("hook_line"),
        "start_s": top.get("start_s"),
        "end_s": top.get("end_s"),
        "feedback": feedback.to_dict(),
    }


# ------------------------------------------------------------- auto-queue
def next_platform_window(
    platform: str, now: datetime, windows: dict[str, int] | None = None
) -> datetime:
    """Next occurrence of a platform's best window (UTC hour).

    ``windows`` maps platform → hour (0–23, UTC); defaults to
    :data:`DEFAULT_WINDOWS` (IG 11, FB 13, YT 15, TikTok 19). If
    today's window already passed, tomorrow's is returned.
    """
    hour = (windows or {}).get(platform, DEFAULT_WINDOWS.get(platform, 12))
    candidate = now.replace(hour=int(hour), minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate


def apply_high_score_rule(session: Session, candidate: ClipCandidate) -> bool:
    """Auto-approve a clip that clears the high-score bar — if the rule
    is on (it is OFF by default, intentionally conservative).

    Bar (from the rule config): adjusted score >= 95 AND hook rating
    >= 9. Returns True when the candidate was auto-approved. This only
    approves the *clip* — a post built from it still needs post-level
    approval before anything can publish, so approval-first publishing
    is preserved.
    """
    if not rule_enabled(session, "auto_approve_high_score"):
        return False
    rule = get_rule(session, "auto_approve_high_score")
    config = (rule.config_json or {}) if rule else {}
    min_score = float(config.get("min_score", 95.0))
    min_hook = float(config.get("min_hook", 9))
    if candidate.score < min_score or (candidate.hook_rating or 0) < min_hook:
        return False
    candidate.status = "approved"
    session.commit()
    log_event(
        session,
        "auto_approve_high_score",
        "auto_approved_high_score",
        {
            "candidate_id": candidate.id,
            "score": candidate.score,
            "hook_rating": candidate.hook_rating,
            "min_score": min_score,
            "min_hook": min_hook,
        },
        job_id=candidate.job_id,
        clip_candidate_id=candidate.id,
    )
    return True


def _queued_today_count(session: Session, platform: str, now: datetime) -> int:
    """Posts already published (or dry-run) + queued for approval today."""
    published = published_today_count(session, None, platform, now)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    queued = (
        session.query(Post)
        .filter_by(platform=platform)
        .filter(
            Post.status.in_(
                [
                    PostStatus.PENDING_APPROVAL.value,
                    PostStatus.APPROVED.value,
                    PostStatus.SCHEDULED.value,
                ]
            )
        )
        .filter(Post.created_at >= start)
        .count()
    )
    return published + queued


def _channel_for(session: Session, platform: str) -> dict:
    """Canonical channel status for one platform (``GET /channels``).

    A platform counts as connected only when the channel says so: an
    OAuth token record, or a configured legacy env-var token. A bare
    ``SocialAccount`` row (legacy account record, ``GET /accounts``)
    does NOT make a channel connected — it carries the label / scopes /
    cap override, but the card must not claim an account it has no
    token for (see :mod:`clipforge.auth.channels`).
    """
    data_dir = Path(os.environ.get("CLIPFORGE_DATA_DIR") or "./data")
    return channel_status(platform, secrets=SecretStore(data_dir), db=session)


def auto_queue_candidate(
    session: Session,
    candidate: ClipCandidate,
    platforms: list[str] | None = None,
) -> list[dict]:
    """Fan-out one approved clip to one pending post per platform.

    Only platforms whose *channel* is connected (``GET /channels``:
    OAuth token or legacy env token) are queued, and only while the
    platform's daily cap (published + queued today, from the channel
    card — provider default or the account's cap override) is not
    reached. Every outcome is logged. Returns one result per
    platform: queued | skipped_cap | skipped_no_account | exists.
    """
    if candidate.status != "approved":
        return [
            {
                "platform": p,
                "status": "skipped_not_approved",
                "reason": f"candidate is '{candidate.status}', not 'approved'",
            }
            for p in (platforms or list(PLATFORMS))
        ]
    rule = get_rule(session, "auto_queue_approved")
    config = (rule.config_json or {}) if rule else {}
    wanted = platforms or config.get("platforms", list(PLATFORMS))
    now = datetime.now(UTC)
    stored_seo = seo_for_candidate(session, candidate)
    results: list[dict] = []
    for platform in wanted:
        base = {"platform": platform, "candidate_id": candidate.id}
        existing = (
            session.query(Post)
            .filter_by(clip_candidate_id=candidate.id, platform=platform)
            .filter(
                Post.status.in_(
                    [
                        PostStatus.PENDING_APPROVAL.value,
                        PostStatus.APPROVED.value,
                        PostStatus.SCHEDULED.value,
                        PostStatus.PUBLISHED.value,
                        PostStatus.DRY_RUN.value,
                    ]
                )
            )
            .first()
        )
        if existing is not None:
            results.append({**base, "status": "exists", "post_id": existing.id})
            continue
        channel = _channel_for(session, platform)
        if channel["status"] != "connected":
            log_event(
                session,
                "auto_queue_approved",
                "queue_skipped_no_account",
                {
                    **base,
                    "reason": f"no connected {platform} channel (status: {channel['status']})",
                },
                job_id=candidate.job_id,
                clip_candidate_id=candidate.id,
            )
            results.append(
                {
                    **base,
                    "status": "skipped_no_account",
                    "reason": f"no connected {platform} channel (status: {channel['status']})",
                }
            )
            continue
        # Legacy account record, if one exists: links the post to the
        # account and carries label/scopes. Not required for queueing.
        account = session.query(SocialAccount).filter_by(provider=platform, status="active").first()
        cap = channel.get("daily_cap")
        if cap is None:
            cap = DAILY_CAPS.get(platform, 10)
        done = _queued_today_count(session, platform, now)
        if done >= cap:
            log_event(
                session,
                "auto_queue_approved",
                "queue_skipped_cap",
                {
                    **base,
                    "cap": cap,
                    "done_today": done,
                    "reason": f"daily cap reached for {platform} ({done}/{cap})",
                },
                job_id=candidate.job_id,
                clip_candidate_id=candidate.id,
            )
            results.append(
                {
                    **base,
                    "status": "skipped_cap",
                    "reason": f"daily cap reached for {platform} ({done}/{cap})",
                }
            )
            continue
        seo = stored_seo.get(platform)
        if not seo:
            keyword = _seo_keyword_for(session, candidate)
            seo = generate_all_seo(
                keyword,
                candidate.hook_line or "",
                candidate.hook_line or "",
                candidate.duration_s or 30.0,
            ).get(platform, {})
        # Staggered scheduling: when the rule is on, each platform's
        # post lands in its own best window (IG 11, FB 13, YT 15,
        # TikTok 19 UTC) instead of all four going out at once. When
        # the rule is off the post has no scheduled time (due as soon
        # as it is approved).
        scheduled_for = None
        stagger_windows = None
        if rule_enabled(session, "staggered_schedule"):
            stagger_rule = get_rule(session, "staggered_schedule")
            stagger_windows = (
                (stagger_rule.config_json or {}).get("windows_utc") if stagger_rule else None
            )
            scheduled_for = next_platform_window(platform, now, stagger_windows)
        post = Post(
            clip_candidate_id=candidate.id,
            account_id=account.id if account else None,
            platform=platform,
            scheduled_for=scheduled_for,
            status=PostStatus.PENDING_APPROVAL.value,
            seo_json=seo,
            video_path=candidate.file_path,
        )
        session.add(post)
        session.commit()
        log_event(
            session,
            "auto_queue_approved",
            "queued_post",
            {
                **base,
                "post_id": post.id,
                "account_id": account.id if account else None,
                "cap": cap,
                "done_today": done + 1,
                "scheduled_for": scheduled_for.isoformat() if scheduled_for else None,
                "staggered": scheduled_for is not None,
            },
            job_id=candidate.job_id,
            clip_candidate_id=candidate.id,
            post_id=post.id,
        )
        results.append(
            {
                **base,
                "status": "queued",
                "post_id": post.id,
                "scheduled_for": scheduled_for.isoformat() if scheduled_for else None,
            }
        )
    return results


def on_candidate_approved(session: Session, candidate: ClipCandidate) -> dict:
    """Automation hooks that run when a candidate is approved.

    Never raises: automation must not break the human approval it
    follows. Failures are logged as automation events with the error.
    Returns what each enabled rule did.
    """
    outcome: dict = {"seo": None, "queue": []}
    try:
        if rule_enabled(session, "auto_seo_on_approve"):
            rule = get_rule(session, "auto_seo_on_approve")
            config = (rule.config_json or {}) if rule else {}
            platforms = config.get("platforms", list(PLATFORMS))
            stored = generate_seo_for_candidate(session, candidate, platforms=platforms)
            log_event(
                session,
                "auto_seo_on_approve",
                "seo_generated",
                {"platforms": sorted(stored), "candidate_id": candidate.id},
                job_id=candidate.job_id,
                clip_candidate_id=candidate.id,
            )
            outcome["seo"] = {"platforms": sorted(stored)}
    except Exception as exc:  # automation must never break approval
        log_event(
            session,
            "auto_seo_on_approve",
            "seo_failed",
            {"error": f"{type(exc).__name__}: {exc}"},
            job_id=candidate.job_id,
            clip_candidate_id=candidate.id,
        )
        outcome["seo"] = {"error": str(exc)}
    try:
        if rule_enabled(session, "auto_queue_approved"):
            outcome["queue"] = auto_queue_candidate(session, candidate)
    except Exception as exc:
        log_event(
            session,
            "auto_queue_approved",
            "queue_failed",
            {"error": f"{type(exc).__name__}: {exc}"},
            job_id=candidate.job_id,
            clip_candidate_id=candidate.id,
        )
        outcome["queue"] = [{"status": "error", "reason": str(exc)}]
    return outcome


def run_automation_pass(session: Session) -> dict:
    """Run one automation pass over approved clips + review jobs.

    For every approved candidate with no posts yet: auto-queue it (per
    rule + caps). For every job in review: pick the next clip (per
    rule). Returns a summary of what happened; everything is also in
    the automation feed.
    """
    ensure_default_rules(session)
    summary: dict = {"queued": [], "picks": [], "skipped": []}
    approved = session.query(ClipCandidate).filter_by(status="approved").all()
    for candidate in approved:
        has_post = session.query(Post).filter_by(clip_candidate_id=candidate.id).first() is not None
        if has_post:
            continue
        if not rule_enabled(session, "auto_queue_approved"):
            summary["skipped"].append(
                {"candidate_id": candidate.id, "reason": "auto_queue_approved disabled"}
            )
            continue
        summary["queued"].append(
            {"candidate_id": candidate.id, "results": auto_queue_candidate(session, candidate)}
        )
    review_jobs = session.query(Job).filter_by(status=JobStatus.REVIEW.value).all()
    for job in review_jobs:
        if not rule_enabled(session, "auto_pick_next_clip"):
            continue
        pick = pick_next_clip(session, job.id)
        if pick is not None:
            summary["picks"].append(pick)
    return summary


__all__ = [
    "PLATFORMS",
    "DEFAULT_RULES",
    "DEFAULT_WINDOWS",
    "FRONTEND_KEYS",
    "BACKEND_KEYS",
    "RULE_DESCRIPTIONS",
    "resolve_kind",
    "ensure_default_rules",
    "get_rule",
    "rule_enabled",
    "log_event",
    "rule_out",
    "event_out",
    "generate_seo_for_candidate",
    "seo_for_candidate",
    "pick_next_clip",
    "next_platform_window",
    "apply_high_score_rule",
    "auto_queue_candidate",
    "on_candidate_approved",
    "run_automation_pass",
]
