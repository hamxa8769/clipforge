# More AI Providers (v0.10.0)

The agent's LLM engine is no longer just Gemini + OpenAI-compatible.
Seven providers plus the built-in rule engine are available, all
managed from Settings → AI engine and the Agent page
(`apps/web/components/LlmProviders.tsx`).

## Providers

| Provider | API | Default model | Key | Get a key |
|---|---|---|---|---|
| Gemini | Google Generative Language | `gemini-2.0-flash` | `AIza…` | https://aistudio.google.com/app/apikey |
| OpenAI | `api.openai.com/v1` | `gpt-4o-mini` | `sk-…` | https://platform.openai.com/api-keys |
| Grok (xAI) | `api.x.ai/v1` (OpenAI-compatible) | `grok-4` | `xai-…` | https://console.x.ai |
| Groq | `api.groq.com/openai/v1` (OpenAI-compatible) | `llama-3.3-70b-versatile` | `gsk_…` | https://console.groq.com/keys |
| OpenRouter | `openrouter.ai/api/v1` (OpenAI-compatible) | `meta-llama/llama-3.3-70b-instruct:free` | `sk-or-…` | https://openrouter.ai/keys |
| Claude (Anthropic) | `api.anthropic.com/v1` (native Messages API) | `claude-3-5-haiku-20241022` | `sk-ant-…` | https://console.anthropic.com/settings/keys |
| Ollama (on this machine) | `http://127.0.0.1:11434/v1` (OpenAI-compatible) | `llama3.1` | none | https://ollama.com |
| Rule-based (no key needed) | built-in templates | — | none | — |

The provider table (`PROVIDERS` in `src/clipforge/llm.py`) follows the
same shape as ClipNova-AI's `PROVIDERS` table in `studio/ai.py` (MIT) —
base URLs, default models and key URLs — re-implemented here for
ClipForge's keyring storage and httpx client.

## Keys

- Every keyed provider stores its key in the OS keyring
  (`app:<provider>:api_key`), exactly like Gemini's. The API only ever
  returns `configured` + last 4 characters.
- Legacy env vars still work as a fallback: `GEMINI_API_KEY`,
  `CLIPFORGE_LLM_API_KEY`, `XAI_API_KEY`, `GROQ_API_KEY`,
  `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`.
- A pasted key's provider is auto-detected from its prefix
  (`sk-or-` → OpenRouter, `gsk_` → Groq, `xai-` → Grok,
  `sk-ant-` → Claude, `AIza` → Gemini).

## Which engine the agent uses

`clipforge.llm.resolve_engine` picks, in order:

1. The explicitly selected provider (`llm_active_provider` in settings,
   set from the UI with `POST /api/llm/active`) — when it is configured.
   Selecting "rules" pins the rule engine; "auto" (default) means step 2.
2. The first configured provider in auto order: Gemini, OpenAI, Grok,
   Groq, OpenRouter, Claude.
3. The rule engine — always available, no key, no internet.

Ollama is never picked automatically: it needs a local server, so it is
only used when explicitly selected. An explicitly selected provider
without a saved key falls back to auto order (the UI disables
unconfigured picks) so the agent never stops working.

If the LLM call fails or returns unusable JSON, the rule engine writes
the copy instead and the result says so (`engine: "rules"`,
`engine_fallback: "<tried>"`) — the agent never fails a clip because
the LLM is down.

## Endpoints

- `GET /api/llm/providers` — all providers with `configured` (masked),
  `active`, `models`, `key_url`, `blurb`.
- `POST /api/llm/{provider}/test {api_key?, model?}` — tests the typed
  key, or the saved key when omitted (Ollama: the local server). Never
  echoes the key; never 500s — failures come back as plain-language
  `{ok: false, model, message}`.
- `POST /api/llm/{provider}/save {api_key?, model?, base_url?}` — saves
  (or clears, with `""`) the key in the keyring; returns the masked view
  only. For Ollama it records model / base URL (no key).
- `POST /api/llm/active {provider}` — pin the agent (`"auto"` /
  `"rules"` also allowed).

The old `POST /api/llm/gemini/test` and `/api/llm/gemini/save` paths
still work — they are served by the generic routes now.

## Limits

- No real provider key exists in this repo, so no provider was
  live-tested with a working key. What was verified: mocked-transport
  unit tests for every provider's request shape (URLs, headers, body,
  response parsing), and real-network checks with fake keys proving the
  requests reach the providers — Groq 401 `invalid_api_key`, Grok 400
  "Incorrect API key provided", Anthropic 401 `invalid x-api-key`,
  OpenRouter 401. A working key still needs one real Test-button press
  on the user's machine.
- Live calls from this Linux VM fail at the sandbox egress (direct TLS
  is blocked, and httpx cannot use the sandbox proxy because of the
  bracketed-IPv6 `NO_PROXY` entry — the pre-existing reason
  `clipforge.llm._new_client` uses `trust_env=False`). That is a VM
  limit, not a code bug; the user's Windows machine connects directly.
