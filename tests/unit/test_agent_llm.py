"""Unit tests — Gemini provider + LLM abstraction (ClipForge Agent v0.5.0).

All HTTP is mocked with httpx.MockTransport: tests NEVER hit the real
Gemini API. The key is never echoed in any result or message.
"""

from __future__ import annotations

import httpx
import pytest

from clipforge.auth.settings_store import SettingsStore
from clipforge.llm import (
    GEMINI_DEFAULT_MODEL,
    GeminiProvider,
    LlmError,
    OpenAICompatibleProvider,
    _new_client,
    generate_copy_with_engine,
    resolve_engine,
)
from clipforge.llm import test_gemini as llm_test_gemini

FAKE_KEY = "AIzaFakeGeminiKey1234"
_OTHER_KEY = "sk-fake-openai-key-9999"


def _gemini_ok_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "generativelanguage.googleapis.com"
    assert request.headers["x-goog-api-key"] == FAKE_KEY
    assert GEMINI_DEFAULT_MODEL in str(request.url)
    return httpx.Response(
        200,
        json={"candidates": [{"content": {"parts": [{"text": "OK"}]}}]},
    )


def _gemini_unauthorized_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"error": {"message": "API key not valid"}})


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- Gemini test
def test_gemini_client_ignores_broken_proxy_env(monkeypatch):
    # Regression: sandbox proxy env vars — a bracketed-IPv6 NO_PROXY
    # entry like "[::1]" — made plain httpx.Client() raise InvalidURL
    # ("Invalid port: ':1]'") at construction time, and
    # POST /api/llm/gemini/test 500'd. The LLM client must ignore
    # proxy env vars entirely (same fix as oauth._new_client).
    monkeypatch.setenv("NO_PROXY", "[::1],localhost")
    monkeypatch.setenv("no_proxy", "[::1],localhost")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8080")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8080")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:8080")
    client = _new_client()
    try:
        assert client._trust_env is False
    finally:
        client.close()


def test_gemini_test_success_mocked():
    result = llm_test_gemini(FAKE_KEY, client=_mock_client(_gemini_ok_handler))
    assert result["ok"] is True
    assert result["model"] == GEMINI_DEFAULT_MODEL
    assert "working" in result["message"].lower()
    assert FAKE_KEY not in result["message"]


def test_gemini_test_rejected_key_plain_language():
    result = llm_test_gemini("bad-key", client=_mock_client(_gemini_unauthorized_handler))
    assert result["ok"] is False
    assert "rejected" in result["message"].lower()
    assert "bad-key" not in result["message"]
    assert "Traceback" not in result["message"]


def test_gemini_test_empty_key():
    result = llm_test_gemini("")
    assert result["ok"] is False
    assert "no gemini api key" in result["message"].lower()


def test_gemini_test_unreachable():
    def _boom(request: httpx.Request):
        raise httpx.ConnectError("connection refused", request=request)

    result = llm_test_gemini(FAKE_KEY, client=_mock_client(_boom))
    assert result["ok"] is False
    assert "could not reach" in result["message"].lower()
    assert FAKE_KEY not in result["message"]


def test_gemini_test_model_not_found():
    def _nf(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "not found"}})

    result = llm_test_gemini(FAKE_KEY, model="gemini-9.9-fake", client=_mock_client(_nf))
    assert result["ok"] is False
    assert "not found" in result["message"].lower()


def test_gemini_generate_text_parses_candidates():
    provider = GeminiProvider(FAKE_KEY)
    text = provider.generate_text("hello", client=_mock_client(_gemini_ok_handler))
    assert text == "OK"


def test_gemini_generate_text_empty_reply_raises_plain_error():
    def _empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": []})

    with pytest.raises(LlmError, match="empty or unreadable"):
        GeminiProvider(FAKE_KEY).generate_text("hello", client=_mock_client(_empty))


# ---------------------------------------------------------------- OpenAI path
def test_openai_provider_generate_text_mocked():
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["Authorization"] == f"Bearer {_OTHER_KEY}"
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello back"}}]})

    provider = OpenAICompatibleProvider(_OTHER_KEY)
    assert provider.generate_text("hi", client=_mock_client(_handler)) == "hello back"


# ---------------------------------------------------------------- resolution
def test_resolve_engine_no_keys_is_rules(tmp_path):
    store = SettingsStore(tmp_path)
    engine, provider = resolve_engine(store)
    assert engine == "rules"
    assert provider is None


def test_resolve_engine_gemini_wins(tmp_path):
    store = SettingsStore(tmp_path)
    store.set_credential("gemini", "api_key", FAKE_KEY)
    store.set_credential("llm", "api_key", _OTHER_KEY)
    engine, provider = resolve_engine(store)
    assert engine == "gemini"
    assert isinstance(provider, GeminiProvider)


def test_resolve_engine_openai_when_no_gemini(tmp_path):
    store = SettingsStore(tmp_path)
    store.set_credential("llm", "api_key", _OTHER_KEY)
    engine, provider = resolve_engine(store)
    assert engine == "openai"
    assert isinstance(provider, OpenAICompatibleProvider)


def test_gemini_key_masked_in_settings_view(tmp_path):
    store = SettingsStore(tmp_path)
    store.set_credential("gemini", "api_key", FAKE_KEY)
    view = store.public_view(env_dry_run=True)["llm"]["gemini"]
    assert view["api_key"]["configured"] is True
    assert view["api_key"]["last4"] == "1234"
    assert FAKE_KEY not in str(view)
    assert view["api_key"]["masked"].endswith("1234")


# ---------------------------------------------------------------- copy engine
def test_generate_copy_with_gemini_engine_mocked():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": (
                                        '{"title": "Ranked games: the clutch", '
                                        '"description": "Ranked games are wild. '
                                        'Watch this clutch.", '
                                        '"hashtags": ["gaming", "clutch"]}'
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    provider = GeminiProvider(FAKE_KEY)
    copy = generate_copy_with_engine(
        "gemini",
        provider,
        "ranked games",
        "Stop making this mistake",
        "gaming",
        "youtube",
        30.0,
        client=_mock_client(_handler),
    )
    assert copy["engine"] == "gemini"
    assert copy["title"] == "Ranked games: the clutch"
    assert copy["hashtags"] == ["gaming", "clutch"]


def test_generate_copy_llm_failure_falls_back_to_rules():
    class _FailingProvider(GeminiProvider):
        def generate_text(self, prompt, *, client=None):  # noqa: D102
            raise LlmError("Could not reach the Gemini API.")

    copy = generate_copy_with_engine(
        "gemini",
        _FailingProvider(FAKE_KEY),
        "ranked games",
        "Stop making this mistake",
        "gaming",
        "youtube",
        30.0,
    )
    assert copy["engine"] == "rules"
    assert copy["engine_fallback"] == "gemini"
    assert copy["title"]


def test_generate_copy_rules_engine_needs_no_provider():
    copy = generate_copy_with_engine(
        "rules",
        None,
        "ranked games",
        "Stop making this mistake",
        "gaming",
        "tiktok",
        30.0,
    )
    assert copy["engine"] == "rules"
    assert copy["title"]
