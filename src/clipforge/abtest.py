"""A/B title testing scaffolding for ClipForge (Phase 2).

Data model + assignment + outcome recording. This is scaffolding on
purpose: titles are created per clip/platform, one variant is assigned
to each scheduled post, and views / completion / viewed% are recorded
on the variant when analytics arrive. There is no automatic winner
picking and no automatic re-posting — a human reads the comparison
(``GET /clips/{id}/title-variants``) and decides.

Assignment is deterministic and balanced: variants are assigned
round-robin by the number of posts already using each variant for
that clip/platform (ties break by creation order), so a small batch
splits evenly instead of all landing on the control. The same post
keeps its variant — assignment happens once, at post creation or via
the explicit assign endpoint, and is stored on the post.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from clipforge.db import ClipCandidate, Post, TitleVariant

PLATFORMS = ("youtube", "instagram", "facebook", "tiktok")


class ABTestError(ValueError):
    """Raised when a title-variant operation is invalid."""


def create_variant(
    session: Session,
    *,
    clip_candidate_id: str,
    platform: str,
    title: str,
    is_control: bool = False,
) -> TitleVariant:
    """Create one title variant for a clip/platform."""
    if platform not in PLATFORMS:
        raise ABTestError(f"platform must be one of {PLATFORMS}, got {platform!r}")
    if not (title or "").strip():
        raise ABTestError("title is required")
    if session.get(ClipCandidate, clip_candidate_id) is None:
        raise ABTestError(f"clip candidate {clip_candidate_id!r} not found")
    if is_control:
        # One control per clip/platform: demote any existing control.
        for other in (
            session.query(TitleVariant)
            .filter_by(clip_candidate_id=clip_candidate_id, platform=platform, is_control=True)
            .all()
        ):
            other.is_control = False
    variant = TitleVariant(
        clip_candidate_id=clip_candidate_id,
        platform=platform,
        title=title.strip(),
        is_control=is_control,
    )
    session.add(variant)
    session.commit()
    return variant


def list_variants(
    session: Session, clip_candidate_id: str, platform: str | None = None
) -> list[TitleVariant]:
    """Variants for a clip, optionally filtered to one platform."""
    query = session.query(TitleVariant).filter_by(clip_candidate_id=clip_candidate_id)
    if platform:
        query = query.filter_by(platform=platform)
    return query.order_by(TitleVariant.created_at.asc()).all()


def assignment_counts(session: Session, clip_candidate_id: str, platform: str) -> dict[str, int]:
    """How many posts already use each variant (for balanced assignment)."""
    counts = {v.id: 0 for v in list_variants(session, clip_candidate_id, platform)}
    for post in (
        session.query(Post).filter_by(clip_candidate_id=clip_candidate_id, platform=platform).all()
    ):
        if post.title_variant_id in counts:
            counts[post.title_variant_id] += 1
    return counts


def pick_variant(session: Session, clip_candidate_id: str, platform: str) -> TitleVariant | None:
    """Pick the least-used variant for a new post (round-robin by count).

    Returns ``None`` when the clip/platform has no variants — callers
    then leave the post on its default SEO title.
    """
    variants = list_variants(session, clip_candidate_id, platform)
    if not variants:
        return None
    counts = assignment_counts(session, clip_candidate_id, platform)
    return min(
        variants,
        key=lambda v: (counts.get(v.id, 0), v.created_at or datetime.min.replace(tzinfo=UTC)),
    )


def assign_variant_to_post(session: Session, post_id: str, variant_id: str) -> Post:
    """Assign a variant to a post (validates clip/platform match)."""
    post = session.get(Post, post_id)
    if post is None:
        raise ABTestError(f"post {post_id!r} not found")
    variant = session.get(TitleVariant, variant_id)
    if variant is None:
        raise ABTestError(f"title variant {variant_id!r} not found")
    if variant.clip_candidate_id != post.clip_candidate_id or variant.platform != post.platform:
        raise ABTestError(
            f"title variant {variant_id!r} does not belong to "
            f"clip {post.clip_candidate_id!r} on {post.platform}"
        )
    post.title_variant_id = variant.id
    if post.seo_json is not None:
        seo = dict(post.seo_json)
        seo["title"] = variant.title
        post.seo_json = seo
    session.commit()
    return post


def record_outcome(
    session: Session,
    variant_id: str,
    *,
    views: int | None = None,
    avg_view_duration_s: float | None = None,
    completion_pct: float | None = None,
    viewed_pct: float | None = None,
) -> TitleVariant:
    """Record analytics outcome fields on a variant.

    Values come from platform analytics (or manual entry) — this
    function only stores them; it never fetches from a platform.
    """
    variant = session.get(TitleVariant, variant_id)
    if variant is None:
        raise ABTestError(f"title variant {variant_id!r} not found")
    if views is not None:
        if views < 0:
            raise ABTestError("views must be >= 0")
        variant.views = views
    for name, value in (
        ("avg_view_duration_s", avg_view_duration_s),
        ("completion_pct", completion_pct),
        ("viewed_pct", viewed_pct),
    ):
        if value is not None:
            if not 0 <= value <= 100 and name != "avg_view_duration_s":
                raise ABTestError(f"{name} must be a percentage 0–100, got {value!r}")
            if name == "avg_view_duration_s" and value < 0:
                raise ABTestError("avg_view_duration_s must be >= 0")
            setattr(variant, name, value)
    variant.outcome_updated_at = datetime.now(UTC)
    session.commit()
    return variant


def variant_results(session: Session, clip_candidate_id: str, platform: str) -> list[dict]:
    """Per-variant comparison table for the review UI / API.

    ``assigned_posts`` counts posts using the variant; ``views`` /
    ``completion_pct`` / ``viewed_pct`` are the recorded outcomes
    (``None`` until analytics arrive — the UI must show 'no data yet',
    never a zero that looks like a measured zero).
    """
    counts = assignment_counts(session, clip_candidate_id, platform)
    out = []
    for v in list_variants(session, clip_candidate_id, platform):
        out.append(
            {
                "id": v.id,
                "clip_candidate_id": v.clip_candidate_id,
                "platform": v.platform,
                "title": v.title,
                "is_control": v.is_control,
                "assigned_posts": counts.get(v.id, 0),
                "views": v.views,
                "avg_view_duration_s": v.avg_view_duration_s,
                "completion_pct": v.completion_pct,
                "viewed_pct": v.viewed_pct,
                "has_outcome": v.outcome_updated_at is not None,
                "created_at": v.created_at.isoformat() if v.created_at else None,
            }
        )
    return out


def stable_variant_for_post(post_id: str, variant_ids: list[str]) -> str | None:
    """Deterministic variant pick from a post id (hash-based, no DB).

    Helper for offline / dry-run previews: the same post id always
    maps to the same variant. The API's balanced ``pick_variant`` is
    preferred for real scheduling; this is the pure fallback.
    """
    if not variant_ids:
        return None
    digest = hashlib.sha256(post_id.encode("utf-8")).hexdigest()
    return variant_ids[int(digest, 16) % len(variant_ids)]


__all__ = [
    "ABTestError",
    "create_variant",
    "list_variants",
    "assignment_counts",
    "pick_variant",
    "assign_variant_to_post",
    "record_outcome",
    "variant_results",
    "stable_variant_for_post",
]
