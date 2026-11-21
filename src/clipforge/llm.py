"""LLM provider abstraction for ClipForge Agent (v0.5.0).

Providers, in the order the agent prefers them:

* ``gemini`` — Google Gemini (``generativelanguage.googleapis.com``).
  First-class: the API key is stored in the OS keyring via
  :class:`clipforge.auth.secrets.SecretStore` (never plain text, never
  returned in full — only ``configured`` + last 4), and
  :func:`test_gemini` actually calls the API and reports success or
  failure in plain language.
* ``openai`` — the existing OpenAI-compatible provider (gpt-4o-mini by
  default), same key storage as before.
* ``rules`` — no key at all; the deterministic rule engine in
  :mod:`clipforge.rule_engine`. The agent always works this way when
  no key is configured, and every response says which engine produced
  the output.

Keys are never logged and never appear in any response, message, or
exception string raised by this module.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import httpx

from clipforge.niches import niche_hashtags, niche_label, normalize_niche
from clipforge.rule_engine import generate_copy as rules_copy

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_DEFAULT_MODEL = "gemini-2.0-flash"
GEMINI_MODELS = ("gemini-2.0-flash", "gemini-1.5-flash")
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"

# A short, fixed probe prompt for the Test button — no user data in it.
_GEMINI_TEST_PROMPT = "Reply with the single word OK and nothing else."
_LLM_TIMEOUT_S = 30.0


class LlmError(ValueError):
    """Plain-language LLM failure — safe to show to the user as-is."""


@dataclass
class LlmProvider:
    """One LLM provider: a name, a model, and a text-completion call."""

    name: str  # "gemini" | "openai"
    model: str

    def generate_text(self, prompt: str, *, client: httpx.Client | None = None) -> str:
        raise NotImplementedError

    def test(self, *, client: httpx.Client | None = None) -> dict:
        raise NotImplementedError


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
    return httpx.Client(timeout=timeout_s)


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
        client = client or _new_client()
        try:
            resp = client.post(
                self._url(),
                headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}]},
            )
        except httpx.HTTPError as exc:
            raise LlmError(
                f"Could not reach the Gemini API: {exc}. "
                "Check your internet connection and test again."
            ) from exc
        finally:
            if own_client:
                client.close()
        if resp.status_code != 200:
            raise LlmError(_http_error_message(resp.status_code, "Gemini"))
        try:
            data = resp.json()
            return str(data["candidates"][0]["content"]["parts"][0]["text"]).strip()
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmError(
                "Gemini answered, but the reply was empty or unreadable — try again."
            ) from exc

    def test(self, *, client: httpx.Client | None = None) -> dict:
        """Call the real Gemini API with the fixed probe prompt.

        Returns ``{"ok", "model", "message"}`` in plain language; the
        key is never echoed, logged, or included in any message.
        """
        try:
            reply = self.generate_text(_GEMINI_TEST_PROMPT, client=client)
        except LlmError as exc:
            return {"ok": False, "model": self.model, "message": str(exc)}
        if reply:
            return {
                "ok": True,
                "model": self.model,
                "message": f"Gemini is working (model {self.model} answered).",
            }
        return {
            "ok": False,
            "model": self.model,
            "message": "Gemini answered, but the reply was empty — try again.",
        }


@dataclass
class OpenAICompatibleProvider(LlmProvider):
    """The existing OpenAI-compatible provider (chat/completions)."""

    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = OPENAI_DEFAULT_MODEL,
    ):
        super().__init__(name="openai", model=model)
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def generate_text(self, prompt: str, *, client: httpx.Client | None = None) -> str:
        own_client = client is None
        client = client or _new_client()
        try:
            resp = client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                },
            )
        except httpx.HTTPError as exc:
            raise LlmError(
                f"Could not reach the LLM endpoint: {exc}. "
                "Check your internet connection and test again."
            ) from exc
        finally:
            if own_client:
                client.close()
        if resp.status_code != 200:
            raise LlmError(_http_error_message(resp.status_code, "LLM"))
        try:
            return str(resp.json()["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmError(
                "The LLM answered, but the reply was empty or unreadable — try again."
            ) from exc

    def test(self, *, client: httpx.Client | None = None) -> dict:
        try:
            reply = self.generate_text(_GEMINI_TEST_PROMPT, client=client)
        except LlmError as exc:
            return {"ok": False, "model": self.model, "message": str(exc)}
        if reply:
            return {
                "ok": True,
                "model": self.model,
                "message": f"LLM is working (model {self.model} answered).",
            }
        return {
            "ok": False,
            "model": self.model,
            "message": "The LLM answered, but the reply was empty — try again.",
        }


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


def resolve_engine(settings_store) -> tuple[str, LlmProvider | None]:
    """Which engine the agent should use, from what is actually configured.

    Gemini wins when its key is saved, then the OpenAI-compatible key,
    then the rule engine (always available, no key). The returned name
    is one of ``"gemini"`` / ``"openai"`` / ``"rules"`` and is what the
    agent reports on every output it produces.
    """
    gemini_key = None
    openai_key = None
    try:
        gemini_key = settings_store.get_credential("gemini", "api_key")
    except Exception:
        gemini_key = None
    try:
        openai_key = settings_store.get_credential("llm", "api_key") or os.environ.get(
            "CLIPFORGE_LLM_API_KEY"
        )
    except Exception:
        openai_key = None
    if gemini_key:
        settings = settings_store.load()
        model = getattr(settings, "gemini_model", "") or GEMINI_DEFAULT_MODEL
        return "gemini", GeminiProvider(gemini_key, model)
    if openai_key:
        settings = settings_store.load()
        return "openai", OpenAICompatibleProvider(
            openai_key, settings.llm_base_url, settings.llm_model
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
    if engine in ("gemini", "openai") and provider is not None:
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
    "GeminiProvider",
    "LlmError",
    "LlmProvider",
    "OPENAI_DEFAULT_MODEL",
    "OpenAICompatibleProvider",
    "generate_copy_with_engine",
    "resolve_engine",
    "test_gemini",
]
