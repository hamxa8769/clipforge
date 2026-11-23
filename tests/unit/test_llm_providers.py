"""Unit tests — more LLM providers (ClipForge v0.10.0).

Grok (xAI), Groq, OpenRouter, Claude (Anthropic) and local Ollama join
Gemini + OpenAI. All HTTP is mocked with httpx.MockTransport: tests
NEVER hit a real provider API, and no real key exists in this repo.
Fake keys below are obviously fake (``...fake`` suffix) and only ever
sent to the mock transport.
"""

from __future__ import annotations

import json

import httpx

from clipforge.auth.settings_store import SettingsStore
from clipforge.llm import (
    PROVIDERS,
    AnthropicProvider,
    LlmError,
    OpenAICompatibleProvider,
    build_provider,
    detect_provider_from_key,
    generate_copy_with_engine,
    provider_key,
    resolve_engine,
)
from clipforge.llm import test_provider as llm_test_provider

GROK_KEY = "xai-fake-test-key"
GROQ_KEY = "gsk_fake-test-key"
OPENROUTER_KEY = "sk-or-fake-test-key"
ANTHROPIC_KEY = "sk-ant-fake-test-key"


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _chat_ok_handler(expected_host: str, expected_key: str | None):
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == expected_host, request.url
        assert request.url.path.endswith("/chat/completions")
        if expected_key is not None:
            assert request.headers["Authorization"] == f"Bearer {expected_key}"
        body = json.loads(request.content.decode("utf-8"))
        assert body["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    return _handler


# ------------------------------------------------------------------ registry
def test_registry_has_all_v010_providers():
    assert set(PROVIDERS) == {
        "gemini",
        "openai",
        "grok",
        "groq",
        "openrouter",
        "anthropic",
        "ollama",
    }
    assert PROVIDERS["grok"].base_url == "https://api.x.ai/v1"
    assert PROVIDERS["groq"].base_url == "https://api.groq.com/openai/v1"
    assert PROVIDERS["openrouter"].base_url == "https://openrouter.ai/api/v1"
    assert PROVIDERS["anthropic"].base_url == "https://api.anthropic.com/v1"
    assert PROVIDERS["anthropic"].kind == "anthropic"
    assert PROVIDERS["ollama"].needs_key is False
    assert PROVIDERS["ollama"].base_url == "http://127.0.0.1:11434/v1"
    for provider_id, info in PROVIDERS.items():
        assert info.key_url.startswith("https://"), provider_id
        assert info.default_model, provider_id
        assert info.models, provider_id


def test_detect_provider_from_key_prefix():
    assert detect_provider_from_key(GROK_KEY) == "grok"
    assert detect_provider_from_key(GROQ_KEY) == "groq"
    assert detect_provider_from_key(OPENROUTER_KEY) == "openrouter"
    assert detect_provider_from_key(ANTHROPIC_KEY) == "anthropic"
    assert detect_provider_from_key("AIza-fake") == "gemini"
    assert detect_provider_from_key("sk-fake") == "openai"
    assert detect_provider_from_key("nonsense") == ""


def test_build_provider_unknown_id():
    try:
        build_provider("nope")
    except ValueError as exc:
        assert "Unknown AI provider" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


# ------------------------------------------------------- OpenAI-compatible
def test_grok_provider_calls_xai_chat_completions():
    provider = OpenAICompatibleProvider("grok", GROK_KEY)
    reply = provider.generate_text(
        "hi", client=_mock_client(_chat_ok_handler("api.x.ai", GROK_KEY))
    )
    assert reply == "OK"
    assert provider.model == "grok-4"


def test_groq_provider_calls_groq_chat_completions():
    provider = OpenAICompatibleProvider("groq", GROQ_KEY)
    reply = provider.generate_text(
        "hi", client=_mock_client(_chat_ok_handler("api.groq.com", GROQ_KEY))
    )
    assert reply == "OK"
    assert provider.model == "llama-3.3-70b-versatile"


def test_openrouter_provider_calls_openrouter():
    provider = OpenAICompatibleProvider("openrouter", OPENROUTER_KEY)
    reply = provider.generate_text(
        "hi", client=_mock_client(_chat_ok_handler("openrouter.ai", OPENROUTER_KEY))
    )
    assert reply == "OK"
    assert ":free" in provider.model


def test_ollama_provider_sends_no_key():
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    provider = OpenAICompatibleProvider("ollama")
    assert provider.generate_text("hi", client=_mock_client(_handler)) == "OK"


def test_ollama_test_reports_not_running_plainly():
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = OpenAICompatibleProvider("ollama")
    result = provider.test(client=_mock_client(_handler))
    assert result["ok"] is False
    assert "Ollama is not answering" in result["message"]
    assert "ollama pull llama3.1" in result["message"]


def test_openai_compatible_rejected_key_is_plain_language():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Incorrect API key"}})

    provider = OpenAICompatibleProvider("grok", "xai-bad-key")
    result = provider.test(client=_mock_client(_handler))
    assert result["ok"] is False
    assert "rejected (HTTP 401)" in result["message"]
    assert "xai-bad-key" not in result["message"]


# ---------------------------------------------------------------- Anthropic
def test_anthropic_provider_uses_messages_api():
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.anthropic.com"
        assert request.url.path.endswith("/messages")
        assert request.headers["x-api-key"] == ANTHROPIC_KEY
        assert request.headers["anthropic-version"] == "2023-06-01"
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "claude-3-5-haiku-20241022"
        assert body["messages"][0]["content"] == "hi"
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "OK"}],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    provider = AnthropicProvider(ANTHROPIC_KEY)
    assert provider.generate_text("hi", client=_mock_client(_handler)) == "OK"


def test_anthropic_provider_test_success():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}]})

    result = AnthropicProvider(ANTHROPIC_KEY).test(client=_mock_client(_handler))
    assert result["ok"] is True
    assert "Claude (Anthropic) is working" in result["message"]


def test_anthropic_empty_text_reply_is_plain_error():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": []})

    provider = AnthropicProvider(ANTHROPIC_KEY)
    try:
        provider.generate_text("hi", client=_mock_client(_handler))
    except LlmError as exc:
        assert "empty or unreadable" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected LlmError")


# ---------------------------------------------------------------- test helper
def test_test_provider_empty_key_is_plain_language():
    result = llm_test_provider("grok", "")
    assert result["ok"] is False
    assert "No Grok" in result["message"]
    assert "paste your key first" in result["message"]


def test_test_provider_ollama_needs_no_key():
    result = llm_test_provider(
        "ollama",
        "",
        client=_mock_client(
            lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
        ),
    )
    assert result["ok"] is True


def test_test_provider_unknown_provider():
    result = llm_test_provider("nope", "key")
    assert result["ok"] is False
    assert "Unknown AI provider" in result["message"]


def test_test_provider_bad_model_name():
    result = llm_test_provider("groq", GROQ_KEY, model="bad model!")
    assert result["ok"] is False
    assert "model name looks wrong" in result["message"]


# ------------------------------------------------------------------ resolve
def test_resolve_engine_grok_when_only_grok_key(tmp_path):
    store = SettingsStore(tmp_path)
    store.set_credential("grok", "api_key", GROK_KEY)
    engine, provider = resolve_engine(store)
    assert engine == "grok"
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.model == "grok-4"


def test_resolve_engine_auto_order_gemini_before_grok(tmp_path):
    store = SettingsStore(tmp_path)
    store.set_credential("gemini", "api_key", "AIza-fake-test-key-1234")
    store.set_credential("grok", "api_key", GROK_KEY)
    engine, _provider = resolve_engine(store)
    assert engine == "gemini"


def test_resolve_engine_explicit_choice_wins(tmp_path):
    store = SettingsStore(tmp_path)
    store.set_credential("gemini", "api_key", "AIza-fake-test-key-1234")
    store.set_credential("groq", "api_key", GROQ_KEY)
    current = store.load()
    current.llm_active_provider = "groq"
    store.save(current)
    engine, provider = resolve_engine(store)
    assert engine == "groq"
    assert provider.model == "llama-3.3-70b-versatile"


def test_resolve_engine_explicit_unconfigured_falls_back_to_auto(tmp_path):
    store = SettingsStore(tmp_path)
    store.set_credential("gemini", "api_key", "AIza-fake-test-key-1234")
    current = store.load()
    current.llm_active_provider = "grok"  # no grok key saved
    store.save(current)
    engine, _provider = resolve_engine(store)
    assert engine == "gemini"


def test_resolve_engine_explicit_ollama_needs_no_key(tmp_path):
    store = SettingsStore(tmp_path)
    current = store.load()
    current.llm_active_provider = "ollama"
    store.save(current)
    engine, provider = resolve_engine(store)
    assert engine == "ollama"
    assert isinstance(provider, OpenAICompatibleProvider)


def test_resolve_engine_ollama_not_picked_automatically(tmp_path):
    # No key saved anywhere and Ollama never auto-wins: without an
    # explicit choice the agent uses the rule engine, it must not
    # assume a local server is running.
    store = SettingsStore(tmp_path)
    engine, provider = resolve_engine(store)
    assert engine == "rules"
    assert provider is None


def test_provider_key_reads_env_fallback(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    assert provider_key(store, "groq") == GROQ_KEY
    # The keyring wins over the env var.
    store.set_credential("groq", "api_key", "gsk_keyring-key")
    assert provider_key(store, "groq") == "gsk_keyring-key"


def test_new_provider_keys_masked_in_settings_view(tmp_path):
    store = SettingsStore(tmp_path)
    store.set_credential("groq", "api_key", "gsk_fake-test-key-9999")
    store.set_credential("anthropic", "api_key", "sk-ant-fake-test-key-8888")
    view = store.public_view(env_dry_run=True)
    assert view["llm"]["active_provider"] == ""
    for service, last4 in (("groq", "9999"), ("anthropic", "8888")):
        v = store.credential_view(service, "api_key")
        assert v["configured"] is True
        assert v["last4"] == last4
        assert "fake-test-key" not in str(v)


# -------------------------------------------------------------------- copy
def test_generate_copy_with_grok_engine_mocked():
    seo_json = (
        '{"title": "Ranked games: the clutch", '
        '"description": "Ranked games are wild. Watch this clutch.", '
        '"hashtags": ["gaming", "clutch"]}'
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": seo_json}}]},
        )

    provider = OpenAICompatibleProvider("grok", GROK_KEY)
    copy = generate_copy_with_engine(
        "grok",
        provider,
        "ranked games",
        "Stop making this mistake",
        "gaming",
        "youtube",
        30.0,
        client=_mock_client(_handler),
    )
    assert copy["engine"] == "grok"
    assert copy["title"] == "Ranked games: the clutch"


def test_generate_copy_anthropic_failure_falls_back_to_rules():
    class _Failing(AnthropicProvider):
        def generate_text(self, prompt, *, client=None):  # noqa: D102
            raise LlmError("Could not reach the Claude API.")

    copy = generate_copy_with_engine(
        "anthropic",
        _Failing(ANTHROPIC_KEY),
        "ranked games",
        "Stop making this mistake",
        "gaming",
        "youtube",
        30.0,
    )
    assert copy["engine"] == "rules"
    assert copy["engine_fallback"] == "anthropic"
    assert copy["title"]
