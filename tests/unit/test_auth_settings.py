"""Unit tests for the app settings store (clipforge.auth.settings_store).

Covers: masked credential views (never a full secret), dry-run override
semantics, settings persistence, and test-connection error handling with
a mocked HTTP transport.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from clipforge.auth.secrets import SecretStore
from clipforge.auth.settings_store import AppSettings, SettingsStore
from clipforge.auth.token_store import TokenRecord, TokenStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SettingsStore:
    monkeypatch.setattr(SecretStore, "_probe_keyring", staticmethod(lambda: None))
    return SettingsStore(tmp_path / "data")


def test_public_view_never_leaks_secrets(store: SettingsStore) -> None:
    store.set_credential("youtube", "client_secret", "SECRET-abcdef1234")
    store.set_credential("llm", "api_key", "sk-abcdef1234567890")
    view = store.public_view(env_dry_run=True)
    text = str(view)
    assert "SECRET-abcdef1234" not in text
    assert "sk-abcdef1234567890" not in text
    assert view["credentials"]["youtube"]["client_secret"]["configured"] is True
    assert view["credentials"]["youtube"]["client_secret"]["last4"] == "1234"
    assert view["llm"]["api_key"]["last4"] == "7890"
    assert view["credentials"]["tiktok"]["client_id"]["configured"] is False
    assert view["secret_backend"] == "encrypted-file"
    assert view["data_dir"].endswith("data")
    assert "ffmpeg" in view
    assert view["monetization_thresholds"], "thresholds must be listed"


def test_dry_run_override_semantics(store: SettingsStore) -> None:
    assert store.effective_dry_run(True) is True  # no override: env wins
    assert store.effective_dry_run(False) is False
    settings = store.load()
    settings.dry_run = False
    store.save(settings)
    assert store.effective_dry_run(True) is False  # settings override wins
    settings.dry_run = True
    store.save(settings)
    assert store.effective_dry_run(False) is True


def test_settings_persist_and_reject_bad_values(store: SettingsStore) -> None:
    settings = AppSettings(dry_run=True, llm_model="gpt-4o", llm_base_url="https://x.example/v1")
    store.save(settings)
    loaded = SettingsStore(store.data_dir).load()
    assert loaded.dry_run is True
    assert loaded.llm_model == "gpt-4o"
    weird = AppSettings.from_dict({"default_privacy": {"youtube": "bogus", "nope": "public"}})
    assert weird.default_privacy["youtube"] == "private"
    assert "nope" not in weird.default_privacy


def test_clear_credential(store: SettingsStore) -> None:
    store.set_credential("tiktok", "client_id", "ck-1234")
    assert store.credential_view("tiktok", "client_id")["configured"] is True
    store.set_credential("tiktok", "client_id", None)
    assert store.credential_view("tiktok", "client_id")["configured"] is False


def test_test_connection_llm_reports_exact_errors(store: SettingsStore) -> None:
    # No key saved at all.
    result = store.test_connection("llm")
    assert result["ok"] is False
    assert "No LLM API key" in result["message"]

    store.set_credential("llm", "api_key", "sk-bad-key")

    def _unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Incorrect API key"}})

    result = store.test_connection(
        "llm", client=httpx.Client(transport=httpx.MockTransport(_unauthorized))
    )
    assert result["ok"] is False
    assert "401" in result["message"]

    def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    result = store.test_connection("llm", client=httpx.Client(transport=httpx.MockTransport(_ok)))
    assert result["ok"] is True

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure", request=request)

    result = store.test_connection("llm", client=httpx.Client(transport=httpx.MockTransport(_boom)))
    assert result["ok"] is False
    assert "dns failure" in result["message"]


def test_test_connection_platform_needs_token_first(store: SettingsStore) -> None:
    # No credentials saved at all: the missing client ID is named first.
    result = store.test_connection("youtube")
    assert result["ok"] is False
    assert "client ID is not saved yet" in result["message"]
    assert result["next_step"]
    # Credentials saved but no token: the plain "not connected" message.
    store.set_credential("youtube", "client_id", "cid")
    store.set_credential("youtube", "client_secret", "csec")
    result = store.test_connection("youtube")
    assert result["ok"] is False
    assert "not connected" in result["message"]
    assert result["next_step"]


def test_test_connection_platform_uses_stored_token(store: SettingsStore) -> None:
    TokenStore(store.secrets).save(
        TokenRecord.from_token_response("youtube", {"access_token": "at-1", "expires_in": 3600})
    )

    def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"id": "UC1", "snippet": {"title": "Ch"}}]})

    result = store.test_connection(
        "youtube", client=httpx.Client(transport=httpx.MockTransport(_ok))
    )
    assert result["ok"] is True
    assert "Ch" in result["message"]


def test_test_connection_unknown_service(store: SettingsStore) -> None:
    result = store.test_connection("myspace")
    assert result["ok"] is False


def test_test_connection_survives_poisoned_proxy_env(
    store: SettingsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed proxy env (bad NO_PROXY) must not 500 Test Connection.

    Regression: plain httpx.Client() reads proxy env at construction and
    raises InvalidURL on this VM's NO_PROXY value. Clients are built with
    trust_env=False, so the test reports an honest not-ok instead.
    """
    store.set_credential("llm", "api_key", "sk-bad-key")
    monkeypatch.setenv("NO_PROXY", "[::1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:bad-port]")
    result = store.test_connection("llm")
    assert result["ok"] is False
    assert "Test failed before reaching the API" not in result["message"]
    assert "Invalid port" not in result["message"]
