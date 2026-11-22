"""User-facing notifications store for ClipForge v0.9.0 (Worker B).

This is the SINGLE notifications store: the ``notifications`` table
plus :func:`emit_notification` with its 24-hour dedupe. Everything
else is an adapter over it:

* :mod:`clipforge.notifications` — the thin adapter Worker C's code
  uses (job queue, worker, installers): ``notify`` / ``notify_best_effort``
  / ``to_ui_item``. It lazy-imports this module to avoid a circular
  import (importing ``clipforge.auth`` pulls in health).
* ``clipforge_api/app.py`` — the HTTP endpoints (``POST /api/notifications``,
  ``GET /api/notifications``, ``.../read``, ``read-all``, ``clear``,
  plus the ``GET /notifications`` UI shape).

Dedupe: :func:`emit_notification` sends at most one *unread*
notification per ``dedupe_key`` per 24 hours, so a flapping channel
cannot spam the activity centre. The channel health monitor uses
``dedupe_key="channel-disconnected:<platform>"`` for exactly that
reason. No secret ever lands in a notification.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from clipforge.db import Notification

LEVELS = ("info", "warning", "error", "success")
_DEDUPE_WINDOW = timedelta(hours=24)


def notification_out(row: Notification) -> dict:
    """Public shape of one notification row (no internal fields)."""
    return {
        "id": row.id,
        "level": row.level,
        "title": row.title,
        "message": row.message,
        "action": row.action,
        "platform": row.platform,
        "job_id": row.job_id,
        "read": bool(row.read),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def emit_notification(
    db: Session,
    *,
    level: str,
    title: str,
    message: str,
    action: str | None = None,
    platform: str | None = None,
    dedupe_key: str | None = None,
    job_id: str | None = None,
) -> dict:
    """Store a notification, or return the existing unread one.

    When ``dedupe_key`` is given and an unread notification with that
    key was created within the last 24 hours, the existing row is
    returned and nothing new is stored. Raises ValueError for a bad
    level or an empty title. ``action`` is a plain in-app path
    (``/channels``, ``/jobs/<id>``); a dict action is normalised to
    its URL by the :mod:`clipforge.notifications` adapter before it
    gets here.
    """
    if level not in LEVELS:
        raise ValueError(f"level must be one of {sorted(LEVELS)}")
    if not (title or "").strip():
        raise ValueError("title must not be empty")
    if dedupe_key:
        since = datetime.now(UTC) - _DEDUPE_WINDOW
        existing = (
            db.query(Notification)
            .filter_by(dedupe_key=dedupe_key, read=False)
            .order_by(Notification.created_at.desc())
            .first()
        )
        if existing is not None and existing.created_at is not None:
            created = existing.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created >= since:
                return notification_out(existing)
    row = Notification(
        level=level,
        title=title.strip(),
        message=message,
        action=action,
        platform=platform,
        dedupe_key=dedupe_key,
        job_id=job_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return notification_out(row)


def list_notifications(db: Session, *, unread_only: bool = False, limit: int = 50) -> list[dict]:
    """Newest-first notifications (``limit`` clamped to 1..200)."""
    limit = max(1, min(200, limit))
    query = db.query(Notification).order_by(Notification.created_at.desc())
    if unread_only:
        query = query.filter_by(read=False)
    return [notification_out(row) for row in query.limit(limit).all()]


def unread_count(db: Session) -> int:
    """How many notifications are still unread."""
    return db.query(Notification).filter_by(read=False).count()


def mark_notification_read(db: Session, notification_id: str) -> dict | None:
    """Mark one notification read; None when it does not exist."""
    row = db.get(Notification, notification_id)
    if row is None:
        return None
    row.read = True
    db.commit()
    return notification_out(row)


def mark_notifications_read(db: Session, ids: list[str] | None = None) -> int:
    """Mark notifications read — the given ids, or all when omitted.

    Returns how many rows changed.
    """
    query = db.query(Notification).filter_by(read=False)
    if ids is not None:
        query = query.filter(Notification.id.in_(list(ids)))
    changed = query.update({"read": True}, synchronize_session=False)
    db.commit()
    return int(changed)


def clear_notifications(db: Session) -> int:
    """Delete every notification; returns how many were removed."""
    removed = db.query(Notification).delete(synchronize_session=False)
    db.commit()
    return int(removed)


__all__ = [
    "LEVELS",
    "clear_notifications",
    "emit_notification",
    "list_notifications",
    "mark_notification_read",
    "mark_notifications_read",
    "notification_out",
    "unread_count",
]
