"""Integration tests for the channels / OAuth / settings endpoints.

Uses FastAPI's TestClient against the real app. Provider HTTP is mocked
by patching the app's ``exchange_code`` / ``fetch_profile`` /
``refresh_access_token`` callables — the endpoint wiring, state handling,
token storage and masked settings responses are all exercised for real.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from clipforge_api.app import app
from fastapi.testclient import TestClient

# `clipforge_api.app` the attribute is the FastAPI instance (the package
# __init__ imports it), so patch the real module object via sys.modules.
app_mod = sys.modules["clipforge_api.app"]


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the API's data dir at a throwaway folder (no repo writes)."""
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    for var in (
        "YOUTUBE_REFRESH_TOKEN",
        "META_ACCESS_TOKEN",
        "TIKTOK_ACCESS_TOKEN",
        "CLIPFORGE_LLM_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_channels_all_not_connected_initially(client: TestClient) -> None:
    resp = client.get("/channels")
    assert resp.status_code == 200
    cards = {c["platform"]: c for c in resp.json()}
    assert set(cards) == {"youtube", "instagram", "facebook", "tiktok", "x"}
    for platform, card in cards.items():
        assert card["status"] == "not_connected", platform
        assert card["daily_cap"] > 0
        assert card["review_notice"], platform  # the honest gate must show
        assert card["fallback"], platform
    assert cards["tiktok"]["review_notice"].startswith("TikTok Content Posting API audit")


def test_settings_view_is_masked_and_complete(client: TestClient) -> None:
    resp = client.get("/settings")
    assert resp.status_code == 200
    view = resp.json()
    assert view["dry_run"] is True  # env default, no override stored
    assert view["dry_run_source"] == "env"
    assert view["llm"]["api_key"]["configured"] is False
    assert view["credentials"]["youtube"]["client_id"]["configured"] is False
    assert view["ffmpeg"]["install_url"] == "https://ffmpeg.org/download.html"
    assert view["monetization_thresholds"]
    assert any(t["platform"] == "youtube" for t in view["monetization_thresholds"])
    assert "secret_backend" in view


def test_settings_save_and_clear_round_trip(client: TestClient) -> None:
    resp = client.put(
        "/settings",
        json={
            "dry_run": False,
            "llm_api_key": "sk-testkey-12345678",
            "credentials": {
                "youtube": {"client_id": "yt-client-1", "client_secret": "yt-secret-9999"}
            },
        },
    )
    assert resp.status_code == 200
    view = resp.json()
    assert view["dry_run"] is False
    assert view["dry_run_source"] == "settings"
    assert view["llm"]["api_key"]["configured"] is True
    assert view["llm"]["api_key"]["last4"] == "5678"
    assert "sk-testkey-12345678" not in resp.text
    assert view["credentials"]["youtube"]["client_secret"]["last4"] == "9999"
    assert "yt-secret-9999" not in resp.text

    # Health now reports the settings override, not the env default.
    assert client.get("/health").json()["dry_run"] is False

    # Clearing a secret removes it.
    resp = client.put("/settings", json={"llm_api_key": ""})
    assert resp.json()["llm"]["api_key"]["configured"] is False


def test_settings_rejects_bad_values(client: TestClient) -> None:
    assert client.put("/settings", json={"llm_base_url": "ftp://x"}).status_code == 422
    assert (
        client.put("/settings", json={"default_privacy": {"youtube": "everyone"}}).status_code
        == 422
    )
    assert (
        client.put("/settings", json={"credentials": {"myspace": {"client_id": "x"}}}).status_code
        == 404
    )


def test_oauth_start_requires_credentials(client: TestClient) -> None:
    resp = client.get("/auth/youtube/start")
    assert resp.status_code == 409
    assert "Settings" in resp.json()["detail"]


def test_oauth_start_authorize_url_is_a_valid_google_url(client: TestClient) -> None:
    """The URL the Connect button opens must be a real Google sign-in URL.

    Regression guard for the v0.4.2 "no link opens" fix: the desktop
    shell only opens http(s) URLs (see `open_external_url` /
    `isExternalUrl`), so a malformed authorize URL would be refused and
    the wizard would report the failure instead of opening anything.
    """
    from urllib.parse import parse_qs, urlparse

    client.put(
        "/settings",
        json={"credentials": {"youtube": {"client_id": "cid", "client_secret": "csec"}}},
    )
    start = client.get("/auth/youtube/start")
    assert start.status_code == 200
    body = start.json()

    parsed = urlparse(body["authorize_url"])
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert parsed.path == "/o/oauth2/v2/auth"
    params = parse_qs(parsed.query)
    assert params["client_id"] == ["cid"]
    assert params["response_type"] == ["code"]
    assert params["access_type"] == ["offline"]  # refresh token, no re-prompt
    assert params["state"] == [body["state"]]
    assert params["state"][0]  # state present (CSRF, single-use)
    # Loopback redirect the wizard shows the user — must be the exact
    # callback the server serves, or Google refuses the grant.
    assert params["redirect_uri"] == [body["redirect_uri"]]
    redirect = urlparse(body["redirect_uri"])
    assert redirect.scheme == "http"
    assert redirect.netloc.startswith("127.0.0.1:")
    assert redirect.path == "/auth/callback/youtube"


def test_oauth_full_flow_connect_refresh_disconnect(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.put(
        "/settings",
        json={"credentials": {"youtube": {"client_id": "cid", "client_secret": "csec"}}},
    )

    def _exchange(platform, code, **kwargs):
        assert code == "auth-code-1"
        return {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}

    def _profile(platform, token, **kwargs):
        assert token == "at-1"
        return {"account_name": "My Channel", "avatar_url": "", "account_ref": "UC123"}

    monkeypatch.setattr(app_mod, "exchange_code", _exchange)
    monkeypatch.setattr(app_mod, "fetch_profile", _profile)

    start = client.get("/auth/youtube/start")
    assert start.status_code == 200
    body = start.json()
    assert "accounts.google.com" in body["authorize_url"]
    assert body["redirect_uri"].endswith("/auth/callback/youtube")

    callback = client.get(
        "/auth/callback/youtube",
        params={"code": "auth-code-1", "state": body["state"]},
    )
    assert callback.status_code == 200
    assert "My Channel" in callback.text

    card = client.get("/channels/youtube").json()
    assert card["status"] == "connected"
    assert card["account_name"] == "My Channel"
    assert card["account_ref"] == "UC123"
    assert card["source"] == "oauth"

    # Reusing the same state must fail (single-use).
    assert (
        client.get(
            "/auth/callback/youtube",
            params={"code": "auth-code-1", "state": body["state"]},
        ).status_code
        == 400
    )

    # Refresh keeps the old refresh token when the provider omits a new one.
    def _refresh(platform, **kwargs):
        assert kwargs["refresh_token"] == "rt-1"
        return {"access_token": "at-2", "expires_in": 3600}

    monkeypatch.setattr(app_mod, "refresh_access_token", _refresh)
    refreshed = client.post("/channels/youtube/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "connected"

    # Disconnect removes the token; the channel reports not connected.
    disconnected = client.post("/channels/youtube/disconnect")
    assert disconnected.status_code == 200
    assert disconnected.json()["status"] == "not_connected"
    assert client.get("/channels/youtube").json()["account_name"] == ""


def test_oauth_callback_refused_by_platform(client: TestClient) -> None:
    resp = client.get("/auth/callback/tiktok", params={"error": "access_denied", "state": "x"})
    assert resp.status_code == 400
    assert "refused" in resp.text


def test_oauth_callback_unknown_platform(client: TestClient) -> None:
    assert (
        client.get("/auth/callback/myspace", params={"code": "x", "state": "y"}).status_code == 404
    )
    assert client.get("/channels/myspace").status_code == 404


def test_test_connection_endpoint_reports_honestly(client: TestClient) -> None:
    # Nothing saved yet: the missing credential is named, with a next step.
    resp = client.post("/settings/test-connection/youtube")
    assert resp.status_code == 502
    assert "client ID is not saved yet" in resp.json()["detail"]
    assert "Next step:" in resp.json()["detail"]
    # Credentials saved but no token: the "not connected" message, still
    # with a plain-language next step — not a bare 500.
    client.put(
        "/settings",
        json={"credentials": {"youtube": {"client_id": "cid", "client_secret": "csec"}}},
    )
    resp = client.post("/settings/test-connection/youtube")
    assert resp.status_code == 502
    assert "not connected" in resp.json()["detail"]
    assert "Next step:" in resp.json()["detail"]
    assert client.post("/settings/test-connection/myspace").status_code == 404
    assert client.post("/settings/test-connection/llm").status_code == 502
    assert "No LLM API key" in client.post("/settings/test-connection/llm").json()["detail"]


def test_legacy_env_token_still_counts_as_connected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "ya29.env-token")
    card = client.get("/channels/youtube").json()
    assert card["status"] == "connected"
    assert card["source"] == "env"
    assert "ya29" not in str(card)  # env token value never leaks into output
    assert os.environ["YOUTUBE_REFRESH_TOKEN"] not in str(card)


def test_channels_canonical_and_accounts_compatible(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /channels is canonical for status; GET /accounts still works.

    After an OAuth connect, both endpoints agree: /channels reports the
    connected card, and the legacy /accounts list shows the same account
    row (status active). Neither endpoint is broken by the other.
    """
    client.put(
        "/settings",
        json={"credentials": {"tiktok": {"client_id": "ck", "client_secret": "cs"}}},
    )
    monkeypatch.setattr(
        app_mod,
        "exchange_code",
        lambda platform, code, **kwargs: {
            "access_token": "at-tt",
            "refresh_token": "rt-tt",
            "expires_in": 86400,
        },
    )
    monkeypatch.setattr(
        app_mod,
        "fetch_profile",
        lambda platform, token, **kwargs: {
            "account_name": "clipforge.demo",
            "avatar_url": "",
            "account_ref": "oid-1",
        },
    )
    start = client.get("/auth/tiktok/start")
    assert start.status_code == 200
    callback = client.get(
        "/auth/callback/tiktok",
        params={"code": "c", "state": start.json()["state"]},
    )
    assert callback.status_code == 200

    card = client.get("/channels/tiktok").json()
    assert card["status"] == "connected"
    assert card["account_name"] == "clipforge.demo"

    accounts = client.get("/accounts").json()
    mine = [a for a in accounts if a["provider"] == "tiktok"]
    assert len(mine) == 1
    assert mine[0]["label"] == "clipforge.demo"
    assert mine[0]["status"] == "active"
    assert mine[0]["token_env_var"] == ""
