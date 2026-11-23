"""LLM provider abstraction for ClipForge Agent (v0.5.0, extended v0.10.0).

Providers, in the order the agent prefers them when no provider is
explicitly selected:

* ``gemini`` — Google Gemini (``generativelanguage.googleapis.com``).
  First-class: the API key is stored in the OS keyring via
  :class:`clipforge.auth.secrets.SecretStore` (never plain text, never
  returned in full — only ``configured`` + last 4), and
  :func:`test_gemini` actually calls the API and reports success or
  failure in plain language.
* ``openai`` — the existing OpenAI-compatible provider (gpt-4o-mini by
  default), same key storage as before.
* ``grok`` — xAI Grok (``api.x.ai/v1``, OpenAI-compatible chat API).
* ``groq`` — Groq (``api.groq.com/openai/v1``, OpenAI-compatible).
* ``openrouter`` — OpenRouter (``openrouter.ai/api/v1``,
  OpenAI-compatible; free ``:free`` models work without billing).
* ``anthropic`` — Anthropic Claude (``api.anthropic.com/v1``,
  native Messages API, not OpenAI-compatible).
* ``ollama`` — a model running on the user's own machine
  (``http://127.0.0.1:11434/v1``, OpenAI-compatible). No key needed;
  only used when explicitly selected — the agent never assumes a
  local server is running.
* ``rules`` — no key at all; the deterministic rule engine in
  :mod:`clipforge.rule_engine`. The agent always works this way when
  no key is configured, and every response says which engine produced
  the output.

The provider table (base URLs, default models, where to get a key)
follows the same shape as ClipNova-AI's ``PROVIDERS`` table in
``studio/ai.py`` (MIT) — re-implemented here for ClipForge's keyring
storage and httpx client. Keys are never logged and never appear in
any response, message, or exception string raised by this module.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import httpx

from clipforge.niches import niche_hashtags, niche_label, normalize_niche
from clipforge.rule_engine import generate_copy as rules_copy

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_DEFAULT_MODEL = "gemini-2.0-flash"
GEMINI_MODELS = ("gemini-2.0-flash", "gemini-1.5-flash")
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"

# A short, fixed probe prompt for the Test button — no user data in it.
_LLM_TEST_PROMPT = "Reply with the single word OK and nothing else."
_LLM_TIMEOUT_S = 30.0

# Model ids are validated with this (same rule as ClipNova's save()):
# letters, digits and . - : / @ only.
_MODEL_RE = re.compile(r"^[\w.\-:/@]+$")


class LlmError(ValueError):
    """Plain-language LLM failure — safe to show to the user as-is."""


@dataclass
class ProviderInfo:
    """One provider's static facts: where it lives, what it costs to ask."""

    id: str
    label: str
    kind: str  # "gemini" | "openai-compatible" | "anthropic"
    base_url: str
    default_model: str
    models: tuple[str, ...]
    needs_key: bool
    key_url: str
    key_prefix: str  # what a key for this provider starts with ("")
    env_var: str  # legacy env fallback ("")
    blurb: str


PROVIDERS: dict[str, ProviderInfo] = {
    "gemini": ProviderInfo(
        id="gemini",
        label="Gemini",
        kind="gemini",
        base_url=GEMINI_API_BASE,
        default_model=GEMINI_DEFAULT_MODEL,
        models=GEMINI_MODELS,
        needs_key=True,
        key_url="https://aistudio.google.com/app/apikey",
        key_prefix="AIza",
        env_var="GEMINI_API_KEY",
        blurb="Google's Gemini — free tier, fast, good at hooks and titles.",
    ),
    "openai": ProviderInfo(
        id="openai",
        label="OpenAI",
        kind="openai-compatible",
        base_url="https://api.openai.com/v1",
        default_model=OPENAI_DEFAULT_MODEL,
        models=("gpt-4o-mini", "gpt-4o"),
        needs_key=True,
        key_url="https://platform.openai.com/api-keys",
        key_prefix="sk-",
        env_var="CLIPFORGE_LLM_API_KEY",
        blurb="OpenAI's chat models — GPT-4o mini is cheap and fast.",
    ),
    "grok": ProviderInfo(
        id="grok",
        label="Grok (xAI)",
        kind="openai-compatible",
        base_url="https://api.x.ai/v1",
        default_model="grok-4",
        models=("grok-4", "grok-4-fast-reasoning"),
        needs_key=True,
        key_url="https://console.x.ai",
        key_prefix="xai-",
        env_var="XAI_API_KEY",
        blurb="xAI's Grok — sharp, witty hooks and titles.",
    ),
    "groq": ProviderInfo(
        id="groq",
        label="Groq",
        kind="openai-compatible",
        base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
        models=("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
        needs_key=True,
        key_url="https://console.groq.com/keys",
        key_prefix="gsk_",
        env_var="GROQ_API_KEY",
        blurb="Groq's ultra-fast inference — free tier, Llama models.",
    ),
    "openrouter": ProviderInfo(
        id="openrouter",
        label="OpenRouter",
        kind="openai-compatible",
        base_url="https://openrouter.ai/api/v1",
        default_model="meta-llama/llama-3.3-70b-instruct:free",
        models=(
            "meta-llama/llama-3.3-70b-instruct:free",
            "google/gemma-3-27b-it:free",
            "deepseek/deepseek-r1:free",
        ),
        needs_key=True,
        key_url="https://openrouter.ai/keys",
        key_prefix="sk-or-",
        env_var="OPENROUTER_API_KEY",
        blurb="OpenRouter — one key, many models, including free ones.",
    ),
    "anthropic": ProviderInfo(
        id="anthropic",
        label="Claude (Anthropic)",
        kind="anthropic",
        base_url="https://api.anthropic.com/v1",
        default_model="claude-3-5-haiku-20241022",
        models=("claude-3-5-haiku-20241022", "claude-3-5-sonnet-20241022"),
        needs_key=True,
        key_url="https://console.anthropic.com/settings/keys",
        key_prefix="sk-ant-",
        env_var="ANTHROPIC_API_KEY",
        blurb="Anthropic's Claude — strong, human-like writing.",
    ),
    "ollama": ProviderInfo(
        id="ollama",
        label="Ollama (on this machine)",
        kind="openai-compatible",
        base_url="http://127.0.0.1:11434/v1",
        default_model="llama3.1",
        models=("llama3.1", "llama3.2", "llava"),
        needs_key=False,
        key_url="https://ollama.com",
        key_prefix="",
        env_var="",
        blurb="A model running on your own machine — free, private, no key. "
        "Needs Ollama running (https://ollama.com) with a model pulled.",
    ),
}

# Auto-selection order when the user has not picked a provider:
# first provider with a saved key wins. Ollama is NOT in this list —
# it needs a local server, so it is only used when explicitly selected.
_AUTO_ORDER = ("gemini", "openai", "grok", "groq", "openrouter", "anthropic")

# Keyring service name per provider id. "openai" keeps the historic
# "llm" service so existing saved keys keep working.
_KEYRING_SERVICE = {
    "gemini": "gemini",
    "openai": "llm",
    "grok": "grok",
    "groq": "groq",
    "openrouter": "openrouter",
    "anthropic": "anthropic",
}


def provider_key(settings_store, provider_id: str) -> str | None:
    """A provider's key: OS keyring first, legacy env var as fallback."""
    info = PROVIDERS.get(provider_id)
    if info is None or not info.needs_key:
        return None
    service = _KEYRING_SERVICE.get(provider_id, provider_id)
    try:
        value = settings_store.get_credential(service, "api_key")
    except Exception:
        value = None
    if value:
        return value
    if info.env_var:
        return os.environ.get(info.env_var) or None
    return None


def provider_model(settings_store, provider_id: str) -> str:
    """The model to use for a provider: saved override, else default."""
    info = PROVIDERS[provider_id]
    try:
        settings = settings_store.load()
        override = (settings.llm_models or {}).get(provider_id) or ""
    except Exception:
        override = ""
    return override or info.default_model


def detect_provider_from_key(key: str) -> str:
    """Guess a provider from a key's prefix ("" when unknown).

    Longest prefix first: ``sk-or-`` (OpenRouter) must win over
    ``sk-`` (OpenAI), and ``sk-ant-`` (Claude) over ``sk-``.
    """
    key = (key or "").strip()
    for provider_id, info in sorted(
        PROVIDERS.items(), key=lambda kv: len(kv[1].key_prefix), reverse=True
    ):
        if info.key_prefix and key.startswith(info.key_prefix):
            return provider_id
    return ""


@dataclass
class LlmProvider:
    """One LLM provider: a name, a model, and a text-completion call."""

    name: str  # provider id, e.g. "gemini" | "grok" | "ollama"
    model: str

    def generate_text(self, prompt: str, *, client: httpx.Client | None = None) -> str:
        raise NotImplementedError

    def test(self, *, client: httpx.Client | None = None) -> dict:
        raise NotImplementedError

    @property
    def label(self) -> str:
        info = PROVIDERS.get(self.name)
        return info.label if info else self.name

    def _test_result(self, reply: str) -> dict:
        if reply:
            return {
                "ok": True,
                "model": self.model,
                "message": f"{self.label} is working (model {self.model} answered).",
            }
        return {
            "ok": False,
            "model": self.model,
            "message": f"{self.label} answered, but the reply was empty — try again.",
        }


def _http_error_message(status: int, provider: str) -> str:
    if status in (400, 401, 403):
        return (
            f"The {provider} API key was rejected (HTTP {status}) — "
            "check the key and save it again."
        )
    if status == 404:
        return f"The {provider} model was not found (HTTP 404) — pick another model in Settings."
    if status == 429:
        return (
            f"The {provider} API said quota / rate limit (HTTP 429) — wait a little and test again."
        )
    return f"The {provider} API answered HTTP {status} — try again later."


def _new_client(timeout_s: float = _LLM_TIMEOUT_S) -> httpx.Client:
    """httpx client that ignores proxy env vars.

    The VM / user machine may carry a malformed proxy config (e.g. a
    bracketed-IPv6 NO_PROXY entry like ``[::1]``) that makes plain
    ``httpx.Client()`` raise InvalidURL at construction time. A desktop
    app calling the Gemini API directly must not fail — or 500 a Test
    button — because of that. Same fix as oauth._new_client.
    """
    try:
        return httpx.Client(timeout=timeout_s, trust_env=False)
    except Exception as exc:
        raise LlmError(
            "Could not start the network client — your machine's proxy "
            f"settings look broken ({exc}). Check the proxy settings and test again."
        ) from exc


def _post_json(
    client: httpx.Client, url: str, *, headers: dict, payload: dict, provider_label: str
) -> dict:
    """POST JSON and return the parsed body, or raise a plain LlmError."""
    try:
        resp = client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise LlmError(
            f"Could not reach the {provider_label} API: {exc}. "
            "Check your internet connection and test again."
        ) from exc
    if resp.status_code != 200:
        raise LlmError(_http_error_message(resp.status_code, provider_label))
    try:
        return resp.json()
    except ValueError as exc:
        raise LlmError(
            f"The {provider_label} API answered, but the reply was unreadable — try again."
        ) from exc


@dataclass
class GeminiProvider(LlmProvider):
    """Google Gemini via the Generative Language REST API."""

    api_key: str = ""

    def __init__(self, api_key: str, model: str = GEMINI_DEFAULT_MODEL):
        super().__init__(name="gemini", model=model)
        self.api_key = api_key

    def _url(self) -> str:
        return f"{GEMINI_API_BASE}/models/{self.model}:generateContent"

    def generate_text(self, prompt: str, *, client: httpx.Client | None = None) -> str:
        own_client = client is None
        try:
            client = client or _new_client()
            data = _post_json(
                client,
                self._url(),
                headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                payload={"contents": [{"parts": [{"text": prompt}]}]},
                provider_label="Gemini",
            )
        finally:
            if own_client and client is not None:
                client.close()
        try:
            return str(data["candidates"][0]["content"]["parts"][0]["text"]).strip()
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmError(
                "Gemini answered, but the reply was empty or unreadable — try again."
            ) from exc

    def test(self, *, client: httpx.Client | None = None) -> dict:
        try:
            reply = self.generate_text(_LLM_TEST_PROMPT, client=client)
        except LlmError as exc:
            return {"ok": False, "model": self.model, "message": str(exc)}
        return self._test_result(reply)


@dataclass
class OpenAICompatibleProvider(LlmProvider):
    """Any provider that speaks OpenAI's /chat/completions API.

    Covers OpenAI itself plus Grok (xAI), Groq, OpenRouter and local
    Ollama — only the base URL, key and model differ.
    """

    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"

    def __init__(
        self,
        provider_id_or_key: str = "openai",
        api_key: str = "",
        base_url: str | None = None,
        model: str | None = None,
    ):
        # Backward compatible with the v0.5.0 call shape
        # ``OpenAICompatibleProvider(api_key)``: a first argument that
        # is not a known provider id is the OpenAI key itself.
        if provider_id_or_key in PROVIDERS:
            provider_id = provider_id_or_key
        else:
            provider_id = "openai"
            api_key = provider_id_or_key or api_key
        info = PROVIDERS[provider_id]
        super().__init__(name=provider_id, model=model or info.default_model)
        self.api_key = api_key
        self.base_url = (base_url or info.base_url).rstrip("/")

    def generate_text(self, prompt: str, *, client: httpx.Client | None = None) -> str:
        own_client = client is None
        try:
            client = client or _new_client()
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            data = _post_json(
                client,
                f"{self.base_url}/chat/completions",
                headers=headers,
                payload={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                },
                provider_label=self.label,
            )
        finally:
            if own_client and client is not None:
                client.close()
        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmError(
                f"The {self.label} API answered, but the reply was empty or unreadable — try again."
            ) from exc

    def test(self, *, client: httpx.Client | None = None) -> dict:
        try:
            reply = self.generate_text(_LLM_TEST_PROMPT, client=client)
        except LlmError as exc:
            # Ollama's failure is usually "not running", not a bad key —
            # say so plainly instead of the generic "could not reach".
            if self.name == "ollama" and "Could not reach" in str(exc):
                return {
                    "ok": False,
                    "model": self.model,
                    "message": (
                        "Ollama is not answering on this machine — start Ollama "
                        "(https://ollama.com) and pull a model first "
                        "(for example: ollama pull llama3.1), then test again."
                    ),
                }
            return {"ok": False, "model": self.model, "message": str(exc)}
        return self._test_result(reply)


@dataclass
class AnthropicProvider(LlmProvider):
    """Anthropic Claude via the native Messages API."""

    api_key: str = ""
    base_url: str = "https://api.anthropic.com/v1"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com/v1",
        model: str | None = None,
    ):
        super().__init__(
            name="anthropic",
            model=model or PROVIDERS["anthropic"].default_model,
        )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def generate_text(self, prompt: str, *, client: httpx.Client | None = None) -> str:
        own_client = client is None
        try:
            client = client or _new_client()
            data = _post_json(
                client,
                f"{self.base_url}/messages",
                headers=self._headers(),
                payload={
                    "model": self.model,
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}],
                },
                provider_label="Claude",
            )
        finally:
            if own_client and client is not None:
                client.close()
        try:
            parts = [
                block.get("text", "")
                for block in data.get("content") or []
                if block.get("type") == "text"
            ]
            text = "".join(parts).strip()
            if not text:
                raise ValueError("empty content")
            return text
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmError(
                "Claude answered, but the reply was empty or unreadable — try again."
            ) from exc

    def test(self, *, client: httpx.Client | None = None) -> dict:
        try:
            reply = self.generate_text(_LLM_TEST_PROMPT, client=client)
        except LlmError as exc:
            return {"ok": False, "model": self.model, "message": str(exc)}
        return self._test_result(reply)


def build_provider(
    provider_id: str,
    api_key: str = "",
    *,
    model: str | None = None,
    base_url: str | None = None,
) -> LlmProvider:
    """Build the provider object for an id (raises ValueError if unknown)."""
    info = PROVIDERS.get(provider_id)
    if info is None:
        raise ValueError(f"Unknown AI provider {provider_id!r}.")
    if info.kind == "gemini":
        return GeminiProvider(api_key, model or info.default_model)
    if info.kind == "anthropic":
        return AnthropicProvider(api_key, base_url or info.base_url, model)
    return OpenAICompatibleProvider(provider_id, api_key, base_url, model)


# ------------------------------------------------------- resolution + copy
def test_gemini(
    api_key: str, model: str = GEMINI_DEFAULT_MODEL, *, client: httpx.Client | None = None
) -> dict:
    """Test a Gemini key without saving it. Never echoes the key."""
    if not api_key or not api_key.strip():
        return {
            "ok": False,
            "model": model,
            "message": "No Gemini API key given — paste your key first.",
        }
    return GeminiProvider(api_key.strip(), model).test(client=client)


def test_provider(
    provider_id: str,
    api_key: str = "",
    model: str | None = None,
    *,
    client: httpx.Client | None = None,
    base_url: str | None = None,
) -> dict:
    """Test any provider's key (or local server) without saving it.

    Never echoes the key. For a provider that needs a key, an empty
    key is a plain-language "paste your key first", not an API call.
    """
    info = PROVIDERS.get(provider_id)
    if info is None:
        return {
            "ok": False,
            "model": model or "",
            "message": f"Unknown AI provider {provider_id!r}.",
        }
    key = (api_key or "").strip()
    if info.needs_key and not key:
        return {
            "ok": False,
            "model": model or info.default_model,
            "message": f"No {info.label} API key given — paste your key first.",
        }
    if model:
        if len(model) > 120 or not _MODEL_RE.match(model):
            return {
                "ok": False,
                "model": model,
                "message": "That model name looks wrong — use letters, digits and . - : / only.",
            }
    return build_provider(provider_id, key, model=model, base_url=base_url).test(client=client)


def resolve_engine(settings_store) -> tuple[str, LlmProvider | None]:
    """Which engine the agent should use, from what is actually configured.

    The user's explicit choice (``llm_active_provider``) wins when
    that provider is configured; otherwise the first configured
    provider in auto order wins; otherwise the rule engine (always
    available, no key). The returned name is a provider id or
    ``"rules"`` and is what the agent reports on every output it
    produces.
    """
    try:
        settings = settings_store.load()
        active_choice = (settings.llm_active_provider or "").strip().lower()
    except Exception:
        active_choice = ""
    if active_choice and active_choice != "auto":
        if active_choice == "rules":
            return "rules", None
        info = PROVIDERS.get(active_choice)
        if info is not None:
            if not info.needs_key:
                base = None
                if active_choice == "ollama":
                    try:
                        base = settings_store.load().ollama_base_url or None
                    except Exception:
                        base = None
                return active_choice, build_provider(
                    active_choice,
                    "",
                    model=provider_model(settings_store, active_choice),
                    base_url=base,
                )
            key = provider_key(settings_store, active_choice)
            if key:
                return active_choice, build_provider(
                    active_choice,
                    key,
                    model=provider_model(settings_store, active_choice),
                )
            # Explicit choice but no key saved: fall through to auto so
            # the agent still works (the UI disables unconfigured picks).
    for provider_id in _AUTO_ORDER:
        key = provider_key(settings_store, provider_id)
        if key:
            return provider_id, build_provider(
                provider_id,
                key,
                model=provider_model(settings_store, provider_id),
            )
    return "rules", None


def _seo_prompt(keyword: str, hook: str, niche: str, platform: str) -> str:
    label = niche_label(niche)
    tags = ", ".join(f"#{t}" for t in niche_hashtags(niche)) or "none"
    return (
        f"You write short-form video SEO for the {label} niche. "
        f"Keyword: {keyword}. Hook line: {hook}. Platform: {platform}. "
        f"Suggested hashtags: {tags}. "
        "Return STRICT JSON only: "
        '{"title": string (short, keyword first), '
        '"description": string (2-3 lines, keyword in the first line), '
        '"hashtags": [string, ...] (2-5, no # prefix)}. '
        "No other text."
    )


def generate_copy_with_engine(
    engine: str,
    provider: LlmProvider | None,
    keyword: str,
    hook: str,
    niche: str = "",
    platform: str = "youtube",
    duration_s: float = 30.0,
    *,
    client: httpx.Client | None = None,
) -> dict:
    """SEO copy from the chosen engine, with a safe rules fallback.

    The returned dict always carries ``engine`` naming what actually
    wrote the copy. If the LLM call fails or returns unusable JSON,
    the rule engine writes the copy instead and ``engine`` says
    ``"rules"`` (with ``engine_fallback`` naming what was tried) —
    the agent never fails a clip because the LLM is down.
    """
    niche = normalize_niche(niche)
    if engine != "rules" and provider is not None:
        try:
            raw = provider.generate_text(_seo_prompt(keyword, hook, niche, platform), client=client)
            parsed = json.loads(raw)
            title = str(parsed.get("title") or "").strip()
            description = str(parsed.get("description") or "").strip()
            hashtags = [str(t).lstrip("#").strip() for t in parsed.get("hashtags") or []]
            if title and description and hashtags:
                variant = rules_copy(keyword, hook, niche, platform, duration_s)
                variant.update(
                    {
                        "title": title[:100],
                        "description": description,
                        "caption": description,
                        "hashtags": hashtags[:5],
                        "engine": engine,
                    }
                )
                return variant
        except (LlmError, ValueError, TypeError):
            pass
        fallback = rules_copy(keyword, hook, niche, platform, duration_s)
        fallback["engine_fallback"] = engine
        return fallback
    return rules_copy(keyword, hook, niche, platform, duration_s)


__all__ = [
    "GEMINI_API_BASE",
    "GEMINI_DEFAULT_MODEL",
    "GEMINI_MODELS",
    "PROVIDERS",
    "AnthropicProvider",
    "GeminiProvider",
    "LlmError",
    "LlmProvider",
    "OPENAI_DEFAULT_MODEL",
    "OpenAICompatibleProvider",
    "build_provider",
    "detect_provider_from_key",
    "generate_copy_with_engine",
    "provider_key",
    "provider_model",
    "resolve_engine",
    "test_gemini",
    "test_provider",
]
