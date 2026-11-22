"""v0.9.0 notifications adapter tests (Worker C).

One store (``clipforge.auth.notifications``) — this module is only
the adapter Worker C's code uses. Covers: levels (unknown → info),
dict actions stored as paths, job linkage, 24 h dedupe, mark read /
mark all read / clear, and the UI shape (tone mapping, link).
"""

from __future__ import annotations

from clipforge.db import Notification, get_session, init_db
from clipforge.notifications import (
    clear_all,
    list_notifications,
    mark_all_read,
    mark_read,
    notify,
    notify_best_effort,
    to_ui_item,
    unread_count,
)

# The unit tests never import the API app (which creates the tables
# at import time), so create them here — idempotent.
init_db()


def _session():
    return get_session()


def test_notify_stores_level_title_message_and_job_link():
    session = _session()
    try:
        row = notify(
            session,
            "error",
            "Job failed",
            "The job stopped: the source file is missing.",
            action={"label": "Open job", "url": "/jobs/abc123"},
            job_id="abc123",
        )
        assert row["level"] == "error"
        assert row["job_id"] == "abc123"
        assert row["action"] == "/jobs/abc123"
        assert unread_count(session) >= 1
        items = list_notifications(session, unread_only=True)
        assert any(n["id"] == row["id"] for n in items)
    finally:
        session.close()


def test_unknown_level_falls_back_to_info():
    session = _session()
    try:
        row = notify(session, "critical", "Something", "happened")
        assert row["level"] == "info"
    finally:
        session.close()


def test_dedupe_sends_one_unread_per_key_per_day():
    session = _session()
    try:
        first = notify(
            session, "error", "FFmpeg install failed", "boom",
            dedupe_key="install-failed:ffmpeg",
        )
        second = notify(
            session, "error", "FFmpeg install failed", "boom again",
            dedupe_key="install-failed:ffmpeg",
        )
        assert first["id"] == second["id"]
        assert (
            session.query(Notification)
            .filter_by(dedupe_key="install-failed:ffmpeg")
            .count()
            == 1
        )
    finally:
        session.close()


def test_mark_read_and_mark_all_read_and_clear():
    session = _session()
    try:
        a = notify(session, "info", "One", "first")
        notify(session, "info", "Two", "second")
        assert mark_read(session, a["id"])["read"] is True
        assert mark_read(session, "does-not-exist") is None
        assert unread_count(session) >= 1
        assert mark_all_read(session) >= 1
        assert unread_count(session) == 0
        assert clear_all(session) >= 2
        assert list_notifications(session) == []
    finally:
        session.close()


def test_notify_best_effort_never_raises():
    # No session, no DB errors surfacing — installers rely on this.
    notify_best_effort("error", "yt-dlp install failed", "boom")
    notify_best_effort("bogus-level", "", "")


def test_to_ui_item_maps_tone_and_link():
    item = to_ui_item(
        {
            "id": "n1",
            "title": "Job failed",
            "message": "the file is missing",
            "level": "error",
            "action": "/jobs/abc123",
            "read": False,
            "created_at": "2026-11-23T00:00:00Z",
        }
    )
    assert item["tone"] == "danger"
    assert item["body"] == "the file is missing"
    assert item["link"] == "/jobs/abc123"
    assert item["read"] is False
    success = to_ui_item(
        {"id": "n2", "title": "Done", "message": "", "level": "success",
         "action": None, "read": True, "created_at": None}
    )
    assert success["tone"] == "success"
    assert success["link"] is None
