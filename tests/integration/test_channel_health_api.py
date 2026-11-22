"""Integration tests for channel reliability (ClipForge v0.9.0, Worker B).

Uses FastAPI's TestClient against the real app. Provider HTTP is
mocked by patching ``clipforge_api.app.refresh_access_token`` /
``exchange_code`` / ``fetch_profile`` (the app-module globals the
refresh + callback paths call) and ``clipforge.auth.health.
refresh_access_token`` (the monitor's path). Token storage, health
cards, notifications and the Meta long-lived exchange are all
exercised for real.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from clipforge_api.app import app
from fastapi.testclient import TestClient

from clipforge.auth import health as health_mod
from clipforge.auth.oauth import OAuthError
from clipforge.auth.secrets import SecretStore
from clipforge.auth.token_store import TokenRecord, TokenStore
from clipforge.db import Notification
from clipforge.db import session_factory as _test_session_factory

app_mod = sys.modules["clipforge_api.app"]


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the API's data dir at a throwaway folder (no repo writes)."""
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(SecretStore, "_probe_keyring", staticmethod(lambda: None))
    for var in (
        "YOUTUBE_REFRESH_TOKEN",
        "META_ACCESS_TOKEN",
        "TIKTOK_ACCESS_TOKEN",
        "CLIPFORGE_LLM_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    # The test database is shared per pytest process (conftest): clear
    # notifications so each test starts from zero unread.
    _db = _test_session_factory()()
    try:
        _db.query(Notification).delete(synchronize_session=False)
        _db.commit()
    finally:
        _db.close()
    yield
    health_mod.stop_all_background_monitors()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _seed_token(client: TestClient, tmp_path: Path, platform: str, **kw) -> TokenRecord:
    """Store a token the way the callback does (encrypted store)."""
    record = TokenRecord(
        platform=platform,
        access_token=kw.get("access_token", "at-1"),
        refresh_token=kw.get("refresh_token", "rt-1"),
        expires_at=datetime.now(UTC) + timedelta(seconds=kw.get("expires_in_s", 3600)),
        scopes=kw.get("scopes", ["youtube.upload"]),
        account_name=kw.get("account_name", "My Channel"),
        account_ref=kw.get("account_ref", "UC1"),
    )
    TokenStore(SecretStore(tmp_path / "data")).save(record)
    return record


# ------------------------------------------------------------------ health endpoint
def test_channels_health_shape_when_nothing_connected(client: TestClient) -> None:
    resp = client.get("/channels/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["summary"]) == {"connected", "expiring_soon", "disconnected", "not_connected"}
    assert body["summary"]["not_connected"] == 5
    assert len(body["platforms"]) == 5
    for entry in body["platforms"]:
        assert entry["health"] == "not_connected"
        assert entry["health_reason"]
        assert entry["health_next_step"]
        assert entry["expires_in_s"] is None
        assert entry["needs_refresh"] is False
        assert entry["refreshed"] is False


def test_channels_cards_carry_health_fields(client: TestClient) -> None:
    cards = {c["platform"]: c for c in client.get("/channels").json()}
    assert cards["youtube"]["health"] == "not_connected"
    assert cards["youtube"]["health_next_step"]
    assert cards["youtube"]["last_error"] is None
    assert cards["youtube"]["checked_at"]


def test_channels_health_refreshes_a_due_token(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_token(client, tmp_path, "youtube", expires_in_s=600)  # due (< 15 min)
    monkeypatch.setattr(
        health_mod,
        "refresh_access_token",
        lambda platform, **kwargs: {"access_token": "at-2", "expires_in": 3600},
    )
    body = client.get("/channels/health").json()
    mine = [p for p in body["platforms"] if p["platform"] == "youtube"][0]
    assert mine["refreshed"] is True
    assert mine["health"] == "connected"
    assert body["summary"]["connected"] >= 1
    assert client.get("/channels/youtube").json()["status"] == "connected"


# ------------------------------------------------------------------ reconnect
def test_reconnect_refreshes_silently_first(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_token(client, tmp_path, "youtube", expires_in_s=-60)  # expired
    seen: dict = {}

    def _refresh(platform, **kwargs):
        seen.update(kwargs)
        return {"access_token": "at-2", "expires_in": 3600}

    monkeypatch.setattr(app_mod, "refresh_access_token", _refresh)
    resp = client.post("/channels/youtube/reconnect")
    assert resp.status_code == 200
    assert resp.json()["status"] == "connected"
    assert resp.json()["health"] == "connected"
    assert seen["refresh_token"] == "rt-1"
    assert client.get("/api/notifications").json()["unread"] == 0  # no alert needed


def test_reconnect_dead_grant_is_409_plus_notification(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_token(client, tmp_path, "youtube", expires_in_s=-60)

    def _dead(platform, **kwargs):
        raise OAuthError("invalid_grant", "refresh token revoked")

    monkeypatch.setattr(app_mod, "refresh_access_token", _dead)
    resp = client.post("/channels/youtube/reconnect")
    assert resp.status_code == 409
    assert "Connect" in resp.json()["detail"]
    notes = client.get("/api/notifications").json()
    assert notes["unread"] == 1
    assert "YouTube" in notes["notifications"][0]["title"]
    assert notes["notifications"][0]["level"] == "error"
    # The token is still stored — reconnecting stays possible.
    assert client.get("/channels/youtube").json()["status"] == "expired"


def test_reconnect_network_failure_is_502_without_notification(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_token(client, tmp_path, "youtube", expires_in_s=-60)

    def _down(platform, **kwargs):
        raise OAuthError("network_error", "connection reset")

    monkeypatch.setattr(app_mod, "refresh_access_token", _down)
    # Three attempts with backoff — the endpoint waits, then reports 502.
    resp = client.post("/channels/youtube/reconnect")
    assert resp.status_code == 502
    assert client.get("/api/notifications").json()["unread"] == 0


def test_reconnect_unknown_channel_is_404(client: TestClient) -> None:
    assert client.post("/channels/youtube/reconnect").status_code == 404


# ------------------------------------------------------------------ notifications endpoints
def test_notifications_create_list_read_round_trip(client: TestClient) -> None:
    created = client.post(
        "/api/notifications",
        json={
            "level": "warning",
            "title": "YouTube disconnected",
            "message": "Token expired.",
            "action": "/channels",
            "platform": "youtube",
            "dedupe_key": "channel-disconnected:youtube",
        },
    )
    assert created.status_code == 201
    assert created.json()["id"]
    # Same dedupe key while unread: no duplicate row.
    again = client.post(
        "/api/notifications",
        json={
            "level": "warning",
            "title": "YouTube disconnected",
            "message": "Token expired.",
            "action": "/channels",
            "platform": "youtube",
            "dedupe_key": "channel-disconnected:youtube",
        },
    )
    assert again.json()["id"] == created.json()["id"]
    listed = client.get("/api/notifications").json()
    assert listed["unread"] == 1
    assert len(listed["notifications"]) == 1
    assert listed["notifications"][0]["platform"] == "youtube"
    read = client.post(f"/api/notifications/{created.json()['id']}/read")
    assert read.status_code == 200
    assert read.json()["read"] is True
    assert client.get("/api/notifications").json()["unread"] == 0
    assert client.post("/api/notifications/does-not-exist/read").status_code == 404


def test_notifications_reject_bad_level_and_empty_title(client: TestClient) -> None:
    assert (
        client.post("/api/notifications", json={"level": "fatal", "title": "x"}).status_code == 422
    )
    assert (
        client.post("/api/notifications", json={"level": "info", "title": "  "}).status_code == 422
    )


def test_notifications_read_all_and_clear(client: TestClient) -> None:
    for i in range(2):
        assert (
            client.post(
                "/api/notifications",
                json={"level": "info", "title": f"note {i}", "message": ""},
            ).status_code
            == 201
        )
    assert client.get("/api/notifications").json()["unread"] == 2
    assert client.post("/api/notifications/read-all").json()["marked_read"] == 2
    assert client.get("/api/notifications").json()["unread"] == 0
    assert client.post("/api/notifications/clear").json()["cleared"] == 2
    assert client.get("/api/notifications").json()["notifications"] == []


# ------------------------------------------------- Meta long-lived + refresh-token
def test_meta_connect_exchanges_for_long_lived_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.put(
        "/settings",
        json={"credentials": {"instagram": {"client_id": "cid", "client_secret": "csec"}}},
    )
    monkeypatch.setattr(
        app_mod,
        "exchange_code",
        lambda platform, code, **kwargs: {
            "access_token": "at-short",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        app_mod,
        "refresh_access_token",
        lambda platform, **kwargs: {"access_token": "at-long", "expires_in": 5184000},
    )
    monkeypatch.setattr(
        app_mod,
        "fetch_profile",
        lambda platform, token, **kwargs: {
            "account_name": "insta.demo",
            "avatar_url": "",
            "account_ref": "ig-1",
        },
    )
    start = client.get("/auth/instagram/start")
    assert start.status_code == 200
    callback = client.get(
        "/auth/callback/instagram",
        params={"code": "c", "state": start.json()["state"]},
    )
    assert callback.status_code == 200
    card = client.get("/channels/instagram").json()
    assert card["status"] == "connected"
    expires = datetime.fromisoformat(card["expires_at"])
    assert expires > datetime.now(UTC) + timedelta(days=30)  # ~60 days, not ~1 hour


def test_reconnect_preserves_refresh_token_when_provider_omits_it(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_token(client, tmp_path, "youtube", refresh_token="rt-keep-me")
    client.put(
        "/settings",
        json={"credentials": {"youtube": {"client_id": "cid", "client_secret": "csec"}}},
    )
    # Re-grant returns no refresh_token (Google does this on re-grants).
    monkeypatch.setattr(
        app_mod,
        "exchange_code",
        lambda platform, code, **kwargs: {"access_token": "at-new", "expires_in": 3600},
    )
    monkeypatch.setattr(
        app_mod,
        "fetch_profile",
        lambda platform, token, **kwargs: {
            "account_name": "My Channel",
            "avatar_url": "",
            "account_ref": "UC1",
        },
    )
    start = client.get("/auth/youtube/start")
    callback = client.get(
        "/auth/callback/youtube",
        params={"code": "c2", "state": start.json()["state"]},
    )
    assert callback.status_code == 200
    stored = TokenStore(SecretStore(tmp_path / "data")).load("youtube")
    assert stored is not None
    assert stored.access_token == "at-new"
    assert stored.refresh_token == "rt-keep-me"  # not overwritten with None
