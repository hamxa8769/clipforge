"""Unit tests for channel reliability (ClipForge v0.9.0, Worker B).

Covers the health assessment, proactive refresh (retry vs definitive
failure), the X Basic-auth refresh, machine-key backup recovery,
token persistence across a simulated restart, and the monitor's
never-silently-drop behaviour. All provider HTTP is mocked — no
network, no real keys.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from clipforge.auth.health import (
    ChannelHealthMonitor,
    ChannelHealthStore,
    assess_health,
    check_platform,
    notify_channel_disconnected,
    refresh_due,
    refresh_with_retry,
)
from clipforge.auth.oauth import OAuthError, refresh_access_token
from clipforge.auth.secrets import SecretStore
from clipforge.auth.settings_store import SettingsStore
from clipforge.auth.token_store import TokenRecord, TokenStore
from clipforge.db import Notification, SocialAccount, init_db, session_factory


@pytest.fixture(autouse=True)
def _file_backend(monkeypatch: pytest.MonkeyPatch):
    """Force the encrypted-file secret backend (no keyring in tests)."""
    monkeypatch.setattr(SecretStore, "_probe_keyring", staticmethod(lambda: None))


def _record(platform: str = "youtube", *, expires_in_s: float = 3600, **kw) -> TokenRecord:
    record = TokenRecord(
        platform=platform,
        access_token=kw.pop("access_token", "at-1"),
        refresh_token=kw.pop("refresh_token", "rt-1"),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_s),
        account_name=kw.pop("account_name", "My Channel"),
        account_ref=kw.pop("account_ref", "UC1"),
    )
    for key, value in kw.items():
        setattr(record, key, value)
    return record


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ assess
def test_assess_connected_expiring_disconnected_not_connected() -> None:
    assert assess_health(_record(expires_in_s=3600), "youtube", now=_now())["health"] == "connected"
    soon = assess_health(_record(expires_in_s=300), "youtube", now=_now())
    assert soon["health"] == "expiring_soon"
    assert "minute" in soon["health_reason"]
    assert soon["needs_refresh"] is True
    expired = assess_health(_record(expires_in_s=-7200), "youtube", now=_now())
    assert expired["health"] == "disconnected"
    assert "expired" in expired["health_reason"].lower()
    assert "Refresh token" in expired["health_next_step"]
    missing = assess_health(None, "youtube", now=_now())
    assert missing["health"] == "not_connected"
    assert "Connect" in missing["health_next_step"]


def test_assess_expired_without_refresh_token_needs_full_reconnect() -> None:
    expired = assess_health(_record(expires_in_s=-60, refresh_token=None), "youtube", now=_now())
    assert expired["health"] == "disconnected"
    assert expired["can_refresh"] is False
    assert "Connect" in expired["health_next_step"]
    assert "Refresh token" not in expired["health_next_step"]


def test_assess_env_account_is_honest_about_no_refresh() -> None:
    env = assess_health(None, "youtube", source="env", now=_now())
    assert env["health"] == "connected"
    assert "environment variable" in env["health_reason"]
    assert env["can_refresh"] is False


def test_refresh_due_boundary_is_15_minutes() -> None:
    assert refresh_due(_record(expires_in_s=16 * 60), now=_now()) is False
    assert refresh_due(_record(expires_in_s=14 * 60), now=_now()) is True
    assert refresh_due(_record(expires_in_s=-10), now=_now()) is True
    assert refresh_due(_record(expires_in_s=3600, expires_at=None), now=_now()) is False
    assert refresh_due(None, now=_now()) is False


def test_token_record_needs_refresh_matches_monitor_window() -> None:
    assert _record(expires_in_s=900).needs_refresh() is True
    assert _record(expires_in_s=901).needs_refresh() is False


# ------------------------------------------------------------------ refresh with retry
def test_refresh_with_retry_recovers_from_transient_failure() -> None:
    calls = {"n": 0}

    def _flaky(platform, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OAuthError("network_error", "connection reset")
        return {"access_token": "at-2", "expires_in": 3600}

    record, error = refresh_with_retry(
        "youtube",
        _record(),
        client_id="cid",
        client_secret="cs",
        refresh_fn=_flaky,
        sleep=lambda s: None,
    )
    assert error is None
    assert record is not None and record.access_token == "at-2"
    assert calls["n"] == 2
    # Account facts + old refresh token survive the refresh.
    assert record.account_name == "My Channel"
    assert record.refresh_token == "rt-1"


def test_refresh_with_retry_does_not_retry_a_dead_grant() -> None:
    calls = {"n": 0}

    def _dead(platform, **kwargs):
        calls["n"] += 1
        raise OAuthError("invalid_grant", "refresh token revoked")

    record, error = refresh_with_retry(
        "youtube",
        _record(),
        client_id="cid",
        client_secret="cs",
        refresh_fn=_dead,
        sleep=lambda s: None,
    )
    assert record is None
    assert error is not None and error.code == "invalid_grant"
    assert calls["n"] == 1  # no pointless retries


def test_refresh_keeps_new_refresh_token_when_provider_rotates_it() -> None:
    def _rotating(platform, **kwargs):
        return {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600}

    record, _ = refresh_with_retry(
        "youtube", _record(), client_id="cid", client_secret="cs", refresh_fn=_rotating
    )
    assert record is not None and record.refresh_token == "rt-2"


# ------------------------------------------------------------------ X refresh uses Basic auth
def test_refresh_x_uses_http_basic_auth_not_body_secret() -> None:
    seen: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen.update(dict(httpx.QueryParams(request.read().decode())))
        return httpx.Response(200, json={"access_token": "x-at-2", "expires_in": 7200})

    token = refresh_access_token(
        "x",
        client_id="x-cid",
        client_secret="x-cs",
        refresh_token="x-rt-1",
        client=httpx.Client(transport=httpx.MockTransport(_handler)),
    )
    assert token["access_token"] == "x-at-2"
    assert seen["grant_type"] == "refresh_token"
    assert seen["refresh_token"] == "x-rt-1"
    assert seen["client_id"] == "x-cid"
    assert "client_secret" not in seen  # must not be in the body for X
    assert seen["auth"] == "Basic " + base64.b64encode(b"x-cid:x-cs").decode()


# ------------------------------------------------------------------ persistence across restart
def test_token_persists_across_simulated_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    TokenStore(SecretStore(data_dir)).save(_record("tiktok", expires_in_s=86400))
    # "Restart": brand-new store instances over the same data dir.
    loaded = TokenStore(SecretStore(data_dir)).load("tiktok")
    assert loaded is not None
    assert loaded.access_token == "at-1"
    assert loaded.refresh_token == "rt-1"  # refresh token survives too
    assert loaded.account_name == "My Channel"
    assert loaded.is_expired() is False


def test_machine_key_backup_recovers_corrupt_primary_key(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = TokenStore(SecretStore(data_dir))
    store.save(_record("youtube"))
    assert (data_dir / "secrets" / ".machine.key.bak").is_file()
    # Corrupt the primary key: previously this silently generated a
    # fresh key and orphaned every file-backend token.
    (data_dir / "secrets" / ".machine.key").write_bytes(b"not-a-valid-key")
    loaded = TokenStore(SecretStore(data_dir)).load("youtube")
    assert loaded is not None
    assert loaded.access_token == "at-1"
    # The primary key was restored from the backup.
    assert (data_dir / "secrets" / ".machine.key").read_bytes() == (
        data_dir / "secrets" / ".machine.key.bak"
    ).read_bytes()


# ------------------------------------------------------------------ monitor
def _monitor_env(tmp_path: Path):
    """(secrets, settings, session, health_store) for monitor tests."""
    data_dir = tmp_path / "data"
    secrets = SecretStore(data_dir)
    settings = SettingsStore(data_dir, secrets)
    settings.set_credential("youtube", "client_id", "cid")
    settings.set_credential("youtube", "client_secret", "cs")
    init_db(f"sqlite:///{tmp_path}/test.db")  # creates the tables
    session = session_factory(f"sqlite:///{tmp_path}/test.db")()
    session.add(SocialAccount(provider="youtube", label="My Channel", status="active"))
    session.commit()
    return secrets, settings, session, ChannelHealthStore(data_dir)


def test_monitor_refreshes_before_expiry(tmp_path: Path) -> None:
    secrets, settings, db, store = _monitor_env(tmp_path)
    TokenStore(secrets).save(_record(expires_in_s=600))  # due (< 15 min)

    def _refresh(platform, **kwargs):
        return {"access_token": "at-2", "expires_in": 3600}

    result = check_platform(
        "youtube",
        secrets=secrets,
        settings_store=settings,
        db=db,
        health_store=store,
        refresh_fn=_refresh,
    )
    assert result["health"] == "connected"
    assert result["refreshed"] is True
    assert TokenStore(secrets).load("youtube").access_token == "at-2"
    assert db.query(Notification).count() == 0  # nothing to report
    db.close()


def test_monitor_definitive_failure_is_honest_disconnect_not_silent_drop(
    tmp_path: Path,
) -> None:
    secrets, settings, db, store = _monitor_env(tmp_path)
    TokenStore(secrets).save(_record(expires_in_s=-60))  # already expired

    def _dead(platform, **kwargs):
        raise OAuthError("invalid_grant", "refresh token revoked")

    result = check_platform(
        "youtube",
        secrets=secrets,
        settings_store=settings,
        db=db,
        health_store=store,
        refresh_fn=_dead,
    )
    assert result["health"] == "disconnected"
    assert "refresh failed" in result["health_reason"].lower()
    assert "Connect" in result["health_next_step"]
    # The token is NOT deleted — the user can still retry / reconnect.
    assert TokenStore(secrets).load("youtube") is not None
    assert db.query(SocialAccount).first().status == "disconnected"
    notes = db.query(Notification).all()
    assert len(notes) == 1
    assert notes[0].level == "error"
    assert "YouTube" in notes[0].title
    assert notes[0].platform == "youtube"
    # A repeat failure does not spam a second notification (24 h dedupe).
    check_platform(
        "youtube",
        secrets=secrets,
        settings_store=settings,
        db=db,
        health_store=store,
        refresh_fn=_dead,
    )
    assert db.query(Notification).count() == 1
    db.close()


def test_monitor_transient_failure_keeps_token_and_records_error(tmp_path: Path) -> None:
    secrets, settings, db, store = _monitor_env(tmp_path)
    TokenStore(secrets).save(_record(expires_in_s=600))  # due, still valid

    def _flaky(platform, **kwargs):
        raise OAuthError("network_error", "connection reset")

    result = check_platform(
        "youtube",
        secrets=secrets,
        settings_store=settings,
        db=db,
        health_store=store,
        refresh_fn=_flaky,
    )
    assert result["health"] == "expiring_soon"  # not falsely disconnected
    assert result["last_error"] == "connection reset"
    assert TokenStore(secrets).load("youtube").access_token == "at-1"
    assert db.query(Notification).count() == 0  # still valid: no alert yet
    db.close()


def test_monitor_expired_without_refresh_token_never_calls_refresh(tmp_path: Path) -> None:
    secrets, settings, db, store = _monitor_env(tmp_path)
    TokenStore(secrets).save(_record(expires_in_s=-60, refresh_token=None))
    calls = {"n": 0}

    def _refresh(platform, **kwargs):
        calls["n"] += 1
        return {"access_token": "at-2", "expires_in": 3600}

    result = check_platform(
        "youtube",
        secrets=secrets,
        settings_store=settings,
        db=db,
        health_store=store,
        refresh_fn=_refresh,
    )
    assert calls["n"] == 0
    assert result["health"] == "disconnected"
    assert "Connect" in result["health_next_step"]
    assert db.query(Notification).count() == 1
    db.close()


def test_notify_channel_disconnected_dedupes_within_a_day(tmp_path: Path) -> None:
    _, _, db, _ = _monitor_env(tmp_path)
    first = notify_channel_disconnected(
        db, platform="tiktok", level="error", title="TikTok disconnected", message="x"
    )
    second = notify_channel_disconnected(
        db, platform="tiktok", level="error", title="TikTok disconnected", message="x"
    )
    assert first is not None
    assert second is not None
    assert first["id"] == second["id"]  # same row, not a duplicate
    assert db.query(Notification).count() == 1
    db.close()


def test_health_store_round_trip(tmp_path: Path) -> None:
    store = ChannelHealthStore(tmp_path / "data")
    store.update("youtube", health="connected", last_error=None, consecutive_failures=0)
    assert store.get("youtube")["health"] == "connected"
    # A second store over the same dir (simulated restart) sees it.
    assert ChannelHealthStore(tmp_path / "data").get("youtube")["health"] == "connected"


def test_monitor_ensure_started_is_idempotent(tmp_path: Path) -> None:
    secrets, settings, db, _ = _monitor_env(tmp_path)
    db.close()
    monitor = ChannelHealthMonitor(
        lambda: secrets,
        lambda: settings,
        lambda: session_factory(f"sqlite:///{tmp_path}/test.db")(),
        interval_s=3600,
        startup_delay_s=3600,
    )
    assert monitor.ensure_started() is True
    assert monitor.ensure_started() is True  # idempotent: still one thread
    monitor.stop()
