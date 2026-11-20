"""DB-backed scheduler (research §8: Postgres-backed, NOT in-process queue.json).

- Due posts: ``status in (approved, scheduled)`` and ``scheduled_for <= now``.
- Idempotency: every Post has a unique ``idempotency_key``; a post that
  already published (or is already ``publishing``) is never re-sent, and
  re-running ``run_due_posts`` on the same DB is a no-op for finished posts.
- Retry with backoff: failed publish -> ``attempts += 1``,
  ``next_retry_at = now + 2**attempts minutes`` (capped at 60 min), status
  back to ``scheduled`` until 5 attempts, then ``failed``.
- Per-account daily caps enforced from the DB before any publish
  (limits.py DAILY_CAPS, overridable per account via
  ``SocialAccount.daily_cap_override``).
- ``run_due_posts(dry_run=True)`` is the entry point; the CLI
  (``python -m clipforge.scheduler``) wraps it. Dry-run is the default:
  the scheduler marks posts ``dry_run`` and never calls a live adapter
  unless ``dry_run=False`` AND the adapter's credentials are present.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clipforge.db import (
    Post,
    PostStatus,
    SchedulerRun,
    SocialAccount,
    init_db,
)
from clipforge.publishing import meta as meta_adapter
from clipforge.publishing import tiktok as tiktok_adapter
from clipforge.publishing import youtube as youtube_adapter
from clipforge.publishing.base import PublishResult
from clipforge.publishing.limits import DAILY_CAPS

MAX_ATTEMPTS = 5
BACKOFF_CAP_MINUTES = 60


def _utcnow() -> datetime:
    return datetime.now(UTC)


def due_posts(session: Session, now: datetime | None = None) -> list[Post]:
    """Posts ready to publish: approved/scheduled and due (or unscheduled)."""
    now = now or _utcnow()
    stmt = (
        select(Post)
        .where(Post.status.in_([PostStatus.APPROVED.value, PostStatus.SCHEDULED.value]))
        .where((Post.scheduled_for.is_(None)) | (Post.scheduled_for <= now))
        .where((Post.next_retry_at.is_(None)) | (Post.next_retry_at <= now))
        .order_by(Post.scheduled_for.asc().nullsfirst(), Post.created_at.asc())
    )
    return list(session.scalars(stmt))


def published_today_count(
    session: Session, account_id: str | None, platform: str, now: datetime | None = None
) -> int:
    """Published (or dry-run published) posts for this account+platform today (UTC)."""
    now = now or _utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = (
        select(func.count())
        .select_from(Post)
        .where(Post.platform == platform)
        .where(Post.status.in_([PostStatus.PUBLISHED.value, PostStatus.DRY_RUN.value]))
        .where(Post.published_at.is_not(None))
        .where(Post.published_at >= start)
    )
    if account_id:
        stmt = stmt.where(Post.account_id == account_id)
    return int(session.scalar(stmt) or 0)


def daily_cap_for(session: Session, post: Post) -> int:
    if post.account_id:
        account = session.get(SocialAccount, post.account_id)
        if account and account.daily_cap_override is not None:
            return int(account.daily_cap_override)
    return DAILY_CAPS.get(post.platform, 10)


def _dispatch(post: Post, dry_run: bool) -> PublishResult:
    """Call the right adapter for a post. SEO payload comes from post.seo_json."""
    seo = post.seo_json or {}
    title = seo.get("title") or seo.get("caption") or "ClipForge clip"
    description = seo.get("description") or seo.get("caption") or title
    if post.platform == "youtube":
        return youtube_adapter.publish(
            title=title,
            description=description,
            video_path=post.video_path or "",
            publish_at=post.scheduled_for,
            dry_run=dry_run,
        )
    if post.platform == "instagram":
        return meta_adapter.publish_instagram(
            caption=description,
            video_url=post.video_url,
            video_path=post.video_path,
            dry_run=dry_run,
        )
    if post.platform == "facebook":
        return meta_adapter.publish_facebook(
            caption=description,
            video_url=post.video_url,
            video_path=post.video_path,
            dry_run=dry_run,
        )
    if post.platform == "tiktok":
        return tiktok_adapter.publish_tiktok(
            title=title,
            video_url=post.video_url,
            video_path=post.video_path,
            mode="upload",
            dry_run=dry_run,
        )  # inbox draft is the safe default
    raise ValueError(f"unknown platform {post.platform!r}")


def run_due_posts(
    session: Session,
    dry_run: bool = True,
    now: datetime | None = None,
    publish_fn: Callable[[Post, bool], PublishResult] | None = None,
) -> list[dict]:
    """Publish every due post. Returns one result dict per due post.

    Idempotent: already-published / dry_run posts are not in the due set,
    so a re-run skips them. Cap-reached posts are left scheduled with an
    error note (not failed) so they are retried tomorrow.

    The run itself is recorded as a :class:`SchedulerRun` row so
    ``GET /scheduler/status`` reports real runs (last run, outcome,
    error) instead of inferring from posts.
    """
    import time as _time

    started = _time.time()
    now = now or _utcnow()
    publish_fn = publish_fn or _dispatch
    run = SchedulerRun(status="running", dry_run=dry_run)
    session.add(run)
    session.commit()
    results: list[dict] = []
    try:
        results = _publish_due(session, publish_fn, dry_run, now)
    except Exception as exc:  # unexpected — the run row records it
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
        run.finished_at = _utcnow()
        run.duration_s = round(_time.time() - started, 2)
        session.commit()
        raise
    failed = [r for r in results if r["status"] == PostStatus.FAILED.value]
    run.status = "failed" if failed else "done"
    run.processed = len(results)
    run.failed_count = len(failed)
    run.error = failed[0].get("error") if failed else None
    run.finished_at = _utcnow()
    run.duration_s = round(_time.time() - started, 2)
    session.commit()
    return results


def _publish_due(
    session: Session,
    publish_fn: Callable[[Post, bool], PublishResult],
    dry_run: bool,
    now: datetime,
) -> list[dict]:
    """The per-post loop of :func:`run_due_posts` (split out so the run
    recording above stays readable)."""
    results: list[dict] = []
    for post in due_posts(session, now):
        cap = daily_cap_for(session, post)
        done = published_today_count(session, post.account_id, post.platform, now)
        if done >= cap:
            post.error = f"daily cap reached for {post.platform} ({done}/{cap}) — retry tomorrow"
            results.append({"post_id": post.id, "status": "skipped_cap", "message": post.error})
            continue
        post.status = PostStatus.PUBLISHING.value
        post.attempts += 1
        session.commit()
        try:
            result = publish_fn(post, dry_run)
        except Exception as exc:  # adapter refusal (not configured, validation, ...)
            result = PublishResult(
                platform=post.platform,
                status="failed",
                dry_run=dry_run,
                error=f"{type(exc).__name__}: {exc}",
            )
        post.publish_result_json = result.to_dict()
        if result.status in {"published", "draft_created"}:
            post.status = PostStatus.PUBLISHED.value
            post.published_at = _utcnow()
            post.error = None
        elif result.status == "dry_run":
            post.status = PostStatus.DRY_RUN.value
            post.published_at = _utcnow()  # dry-run "published" for cap counting
            post.error = None
        else:
            post.error = result.error or result.message or "publish failed"
            if post.attempts >= MAX_ATTEMPTS:
                post.status = PostStatus.FAILED.value
                post.next_retry_at = None
            else:
                backoff = min(2**post.attempts, BACKOFF_CAP_MINUTES)
                post.status = PostStatus.SCHEDULED.value
                post.next_retry_at = _utcnow() + timedelta(minutes=backoff)
        session.commit()
        results.append(
            {
                "post_id": post.id,
                "status": post.status,
                "platform": post.platform,
                "dry_run": dry_run,
                "attempts": post.attempts,
                "provider_post_id": result.provider_post_id,
                "error": post.error,
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClipForge scheduler — run due posts")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Live publish (default is dry-run). Requires credentials.",
    )
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)
    dry_run = not args.live
    if args.live and os.environ.get("CLIPFORGE_DRY_RUN", "true").lower() in {"1", "true", "yes"}:
        print(
            "Refusing --live: CLIPFORGE_DRY_RUN is still true. "
            "Set CLIPFORGE_DRY_RUN=false to allow live publishing.",
            file=sys.stderr,
        )
        return 2
    engine = init_db(args.database_url)
    from sqlalchemy.orm import sessionmaker as _sm

    session = _sm(bind=engine)()
    try:
        results = run_due_posts(session, dry_run=dry_run)
    finally:
        session.close()
    for row in results:
        print(row)
    print(f"{len(results)} due post(s) processed (dry_run={dry_run})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "run_due_posts",
    "due_posts",
    "published_today_count",
    "daily_cap_for",
    "main",
    "MAX_ATTEMPTS",
]
