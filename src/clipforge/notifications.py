"""Notifications adapter for ClipForge v0.9.0 (Worker C).

The store is the single one owned by :mod:`clipforge.auth.notifications`
(Worker B: the ``notifications`` table + ``emit_notification`` with its
24 h dedupe). This module is only the thin adapter Worker C's code
uses — the job queue, the worker, the installers, and Worker B's
channel health monitor (``clipforge.auth.health`` imports
:func:`notify` / :func:`notification_out` from here) — so every
failure path writes to the same table the header bell reads. There is
exactly one notifications store; nothing here creates a second one.

Import shape matters: ``clipforge.auth.health`` imports this module at
its top level, and importing ``clipforge.auth.notifications`` runs the
``clipforge.auth`` package ``__init__`` (which imports health). So
this module must NOT import ``clipforge.auth.notifications`` at its
top level — every store access below is a lazy import inside the
function, after the package is fully loaded. A top-level import here
would be a circular import and break the server at startup.

Job notifications pass ``action={"label": ..., "url": ...}``; the
adapter stores the URL string (the table's ``action`` column is a
path, e.g. ``/jobs/<id>``) and links the row to the job via
``job_id``. ``notify`` never raises for a bad level — unknown levels
fall back to ``info`` — and :func:`notify_best_effort` never raises
at all, for background threads (installers) that hold no session.

API contract (docs/API-CONTRACT.md §6, implemented in
``clipforge_api/app.py`` next to Worker B's ``/api/notifications``
endpoints)::

    POST /api/notifications  {level, title, message, action?, job_id?,
                             platform?, dedupe_key?}
    GET  /api/notifications  ?unread_only=&limit=  -> {notifications, unread}
    POST /api/notifications/{id}/read
    POST /api/notifications/read-all
    POST /api/notifications/clear
    GET  /notifications      -> NotificationItem[] (UI shape, §6)
    POST /notifications/read {ids?: string[]} -> {read: number}
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# Same values as clipforge.auth.notifications.LEVELS (kept local so
# this module stays importable before the auth package loads — see
# the module docstring).
LEVELS = ("info", "warning", "error", "success")

__all__ = [
    "LEVELS",
    "clear_all",
    "list_notifications",
    "mark_all_read",
    "mark_read",
    "notification_out",
    "notify",
    "notify_best_effort",
    "to_ui_item",
    "unread_count",
]


def _store():
    """The single store (lazy import — see the module docstring)."""
    from clipforge.auth import notifications as store  # noqa: PLC0415

    return store


def _action_string(action: dict | str | None) -> str | None:
    """Normalise an action to the table's plain-path string.

    A dict ``{"label": ..., "url": ...}`` stores its URL; a string is
    used as-is. The label is UI dressing — the stored path is what
    the bell links to.
    """
    if action is None:
        return None
    if isinstance(action, str):
        return action or None
    if isinstance(action, dict):
        url = action.get("url")
        return str(url) if url else None
    return None


def notification_out(row) -> dict:
    """Public shape of one notification row (the store's serializer)."""
    return _store().notification_out(row)


def notify(
    session: Session,
    level: str,
    title: str,
    message: str,
    action: dict | str | None = None,
    job_id: str | None = None,
    platform: str | None = None,
    dedupe_key: str | None = None,
) -> dict:
    """Create one notification in the shared store.

    Unknown levels fall back to ``info`` (never raise — a failed
    notify must never fail the job that triggered it).
    """
    return _store().emit_notification(
        session,
        level=level if level in LEVELS else "info",
        title=title,
        message=message,
        action=_action_string(action),
        platform=platform,
        dedupe_key=dedupe_key,
        job_id=job_id,
    )


def notify_best_effort(
    level: str,
    title: str,
    message: str,
    action: dict | str | None = None,
    job_id: str | None = None,
    dedupe_key: str | None = None,
) -> None:
    """Create a notification without a caller session; never raises.

    For background threads (installers, updater checks) that hold no
    DB session. When the database is unavailable the notification is
    dropped silently — a failed notify must never break the caller.
    """
    try:
        from clipforge.db import get_session  # noqa: PLC0415 (lazy: DB only)

        session = get_session()
        try:
            notify(
                session,
                level,
                title,
                message,
                action=action,
                job_id=job_id,
                dedupe_key=dedupe_key,
            )
        finally:
            session.close()
    except Exception:
        pass


def list_notifications(session: Session, limit: int = 100, unread_only: bool = False) -> list[dict]:
    """Newest first, capped at ``limit`` (1..200)."""
    return _store().list_notifications(session, unread_only=unread_only, limit=limit)


def unread_count(session: Session) -> int:
    return _store().unread_count(session)


def mark_read(session: Session, notification_id: str) -> dict | None:
    return _store().mark_notification_read(session, notification_id)


def mark_all_read(session: Session) -> int:
    """Mark every notification read; returns how many changed."""
    return _store().mark_notifications_read(session)


def clear_all(session: Session) -> int:
    """Delete every notification; returns how many were removed."""
    return _store().clear_notifications(session)


def to_ui_item(row: dict) -> dict:
    """Map a stored notification to the UI shape (API-CONTRACT §6).

    ``{id, title, body, tone, read, created_at, link}`` where tone
    maps the stored level (error → danger, everything else passes
    through; the frontend normalises unknown tones to info anyway).
    """
    level = row.get("level") or "info"
    tone = {"error": "danger"}.get(level, level)
    action = row.get("action")
    link = None
    if isinstance(action, str) and action.startswith("/"):
        link = action
    elif isinstance(action, str) and action.startswith("{"):
        try:
            parsed = json.loads(action)
            if isinstance(parsed, dict) and str(parsed.get("url") or "").startswith("/"):
                link = str(parsed["url"])
        except (ValueError, TypeError):
            link = None
    return {
        "id": row["id"],
        "title": row["title"],
        "body": row.get("message") or "",
        "tone": tone,
        "read": bool(row.get("read")),
        "created_at": row.get("created_at"),
        "link": link,
    }
