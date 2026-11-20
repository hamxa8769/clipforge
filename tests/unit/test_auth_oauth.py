"""Unit tests for OAuth flows (clipforge.auth.oauth / token_store).

All provider HTTP is mocked with httpx.MockTransport — no network, no
real keys. Covers authorize URLs, state handling, code exchange, refresh
(incl. Meta's long-lived exchange), profile parsing, error mapping and
token expiry.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from clipforge.auth.oauth import (
    OAuthError,
    StateStore,
    build_authorize_url,
    check_health,
    exchange_code,
    fetch_profile,
    refresh_access_token,
)
from clipforge.auth.secrets import SecretStore
from clipforge.auth.token_store import TokenRecord, TokenStore


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ authorize URL
def test_youtube_authorize_url_requests_offline_access() -> None:
    url = build_authorize_url(
        "youtube",
        client_id="cid",
        redirect_uri="http://127.0.0.1:8000/auth/callback/youtube",
        state="s1",
    )
    assert "accounts.google.com" in url
    assert "access_type=offline" in url
    assert "youtube.upload" in url


def test_tiktok_authorize_url_uses_comma_scopes() -> None:
    url = build_authorize_url(
        "tiktok",
        client_id="ck",
        redirect_uri="http://127.0.0.1:8000/auth/callback/tiktok",
        state="s1",
    )
    assert "scope=user.info.basic%2Cvideo.upload" in url


def test_unknown_platform_raises() -> None:
    with pytest.raises(ValueError, match="unknown platform"):
        build_authorize_url("myspace", client_id="x", redirect_uri="http://x/", state="s")


# ------------------------------------------------------------------ state
def test_state_store_single_use_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    states = StateStore()
    state = states.new("youtube")
    states.consume(state, "youtube")
    with pytest.raises(OAuthError, match="already used"):
        states.consume(state, "youtube")
    other = states.new("tiktok")
    with pytest.raises(OAuthError, match="different platform"):
        states.consume(other, "youtube")
    stale = states.new("youtube")
    import time as _time

    states._states[stale] = ("youtube", _time.time() - 1)  # already expired
    with pytest.raises(OAuthError, match="expired"):
        states.consume(stale, "youtube")


# ------------------------------------------------------------------ exchange / refresh
def _token_handler(request: httpx.Request) -> httpx.Response:
    body = dict(httpx.QueryParams(request.read().decode()))
    if body.get("code") == "bad-code":
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "expired"})
    if body.get("grant_type") == "refresh_token":
        return httpx.Response(200, json={"access_token": "new-access", "expires_in": 3600})
    if body.get("grant_type") == "fb_exchange_token":
        return httpx.Response(200, json={"access_token": "long-lived", "expires_in": 5184000})
    return httpx.Response(
        200,
        json={"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600},
    )


def test_exchange_code_success() -> None:
    token = exchange_code(
        "youtube",
        "good-code",
        client_id="cid",
        client_secret="cs",
        redirect_uri="http://127.0.0.1:8000/auth/callback/youtube",
        client=_client(_token_handler),
    )
    assert token["access_token"] == "at-1"
    assert token["refresh_token"] == "rt-1"


def test_exchange_code_invalid_grant_maps_to_reconnect_message() -> None:
    with pytest.raises(OAuthError) as excinfo:
        exchange_code(
            "youtube",
            "bad-code",
            client_id="cid",
            client_secret="cs",
            redirect_uri="http://127.0.0.1:8000/auth/callback/youtube",
            client=_client(_token_handler),
        )
    assert excinfo.value.code == "invalid_grant"
    assert "Connect again" in excinfo.value.message


def test_exchange_code_network_error() -> None:
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(OAuthError) as excinfo:
        exchange_code(
            "youtube",
            "code",
            client_id="cid",
            client_secret="cs",
            redirect_uri="http://127.0.0.1:8000/auth/callback/youtube",
            client=_client(_boom),
        )
    assert excinfo.value.code == "network_error"


def test_refresh_youtube_uses_refresh_token_grant() -> None:
    seen: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        seen.update(dict(httpx.QueryParams(request.read().decode())))
        return httpx.Response(200, json={"access_token": "new-access", "expires_in": 3600})

    token = refresh_access_token(
        "youtube",
        client_id="cid",
        client_secret="cs",
        refresh_token="rt-1",
        client=_client(_capture),
    )
    assert seen["grant_type"] == "refresh_token"
    assert seen["refresh_token"] == "rt-1"
    assert token["access_token"] == "new-access"


def test_refresh_meta_exchanges_for_long_lived_token() -> None:
    seen: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        seen.update(dict(httpx.QueryParams(request.read().decode())))
        return httpx.Response(200, json={"access_token": "long-lived", "expires_in": 5184000})

    token = refresh_access_token(
        "facebook",
        client_id="cid",
        client_secret="cs",
        access_token="short-lived",
        client=_client(_capture),
    )
    assert seen["grant_type"] == "fb_exchange_token"
    assert seen["fb_exchange_token"] == "short-lived"
    assert token["access_token"] == "long-lived"


def test_refresh_without_refresh_token_is_honest() -> None:
    with pytest.raises(OAuthError, match="no refresh token"):
        refresh_access_token("youtube", client_id="cid", client_secret="cs")


# ------------------------------------------------------------------ profile / health
def test_fetch_profile_youtube() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer at-1"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "UC123",
                        "snippet": {
                            "title": "My Channel",
                            "thumbnails": {"default": {"url": "https://i.yt/avatar.jpg"}},
                        },
                    }
                ]
            },
        )

    profile = fetch_profile("youtube", "at-1", client=_client(_handler))
    assert profile == {
        "account_name": "My Channel",
        "avatar_url": "https://i.yt/avatar.jpg",
        "account_ref": "UC123",
    }


def test_fetch_profile_tiktok() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "user": {
                        "open_id": "oid-1",
                        "display_name": "clipforge.demo",
                        "avatar_url": "https://tiktok/avatar.png",
                    }
                }
            },
        )

    profile = fetch_profile("tiktok", "at-1", client=_client(_handler))
    assert profile["account_name"] == "clipforge.demo"
    assert profile["account_ref"] == "oid-1"


def test_fetch_profile_meta_unauthorized_maps_error() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "Invalid OAuth access token.", "code": 190}},
        )

    with pytest.raises(OAuthError) as excinfo:
        fetch_profile("facebook", "bad", client=_client(_handler))
    assert "Invalid OAuth access token" in excinfo.value.message


def test_check_health_reports_ok_and_failure() -> None:
    def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"id": "UC1", "snippet": {"title": "Ch"}}]})

    assert check_health("youtube", "at-1", client=_client(_ok))["ok"] is True

    def _bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": 401, "message": "Unauthorized"}})

    result = check_health("youtube", "bad", client=_client(_bad))
    assert result["ok"] is False
    assert "Unauthorized" in result["message"]


# ------------------------------------------------------------------ token records
def test_token_record_expiry_with_skew() -> None:
    fresh = TokenRecord(
        platform="youtube",
        access_token="a",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert fresh.is_expired() is False
    stale = TokenRecord(
        platform="youtube",
        access_token="a",
        expires_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    assert stale.is_expired() is True
    no_expiry = TokenRecord(platform="facebook", access_token="a", expires_at=None)
    assert no_expiry.is_expired() is False  # health check decides, not the clock


def test_token_store_round_trip_encrypted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SecretStore, "_probe_keyring", staticmethod(lambda: None))
    store = TokenStore(SecretStore(tmp_path / "data"))
    record = TokenRecord.from_token_response(
        "tiktok", {"access_token": "at", "refresh_token": "rt", "expires_in": 86400}
    )
    record.account_name = "clipforge.demo"
    store.save(record)
    loaded = store.load("tiktok")
    assert loaded is not None
    assert loaded.access_token == "at"
    assert loaded.account_name == "clipforge.demo"
    assert loaded.is_expired() is False
    assert store.load("youtube") is None
    assert store.delete("tiktok") is True
    assert store.load("tiktok") is None


def test_token_record_from_response_keeps_provider_scopes() -> None:
    record = TokenRecord.from_token_response("youtube", {"access_token": "a", "expires_in": 3600})
    assert "https://www.googleapis.com/auth/youtube.upload" in record.scopes
    assert record.expires_at is not None
    data = json.loads(json.dumps(record.to_dict()))
    assert TokenRecord.from_dict(data).access_token == "a"
