# UI patterns reused vs built fresh (v0.3.0 dashboard / automation UX)

Design reference only — **no code was copied, no repo was forked**.
Patterns below were read from public READMEs / docs / reviews and
re-implemented from scratch in ClipForge's own Next.js + Tailwind
components. Licence note: `zhouxiaoka/autoclip` is MIT (reuse would be
permitted); `gitroomhq/postiz-app` is **AGPL-3.0 — do NOT fork** for a
closed SaaS (research report §9). Nothing in this UI is derived from
Postiz code.

Sources consulted (20–21 Nov 2026):
- Research report §9: `~/workspace/research_notes/social-clipping-automation-tool-research-20261120-1033/report.md`
- `zhouxiaoka/autoclip` README-EN (web UI flow, sample project, per-platform publish pack)
- Postiz calendar docs: https://docs.postiz.com/general/calendar/overview
- Postiz 2026 review (dashboard / calendar description): https://nubiapage.com/postiz-2026-review-app-pricing-alternatives-login-user-experience-and-faqs/
- Postiz v2 redesign PR #893 (design-system notes: unified modals, theme tokens, collapsible sidebars)

## Borrowed patterns (design idea → where it landed in ClipForge)

| # | Pattern | Source | How ClipForge uses it |
|---|---|---|---|
| 1 | **Linear pipeline strip**: Import → Transcribe → Score → Clips → Export, with per-step state | autoclip README ("Import video → Subtitles / transcription → AI analysis and scoring → Clips and collections → Export") | `components/PipelineSteps.tsx`: Ingest → Transcribe → Score → Review → SEO → Approve → Schedule → Publish, with done/active/waiting states, on the job detail page and as a progress bar on dashboard job rows. Extended with per-stage status, error + retry (autoclip has no equivalent). |
| 2 | **Empty state with a sample / next step** ("try the sample project" when no projects exist) | autoclip desktop first-run | Dashboard "No channel connected yet" + Channels "No accounts configured" empty states, each with one clear action (Connect a channel). Demo data is used the same way autoclip uses its bundled sample — always labelled, never presented as live. |
| 3 | **Scored clip cards**: score + hook rating + 1-sentence reason, per-platform copy pack (titles/descriptions/hashtags) | autoclip (up to 10 scored clips per platform, covers + copy); research §2 stage 3 (score 0–100, hook 1–10, reason) | Already existed in `ReviewClient`; v0.3.0 keeps it and adds the pipeline above it. No change to the pattern itself. |
| 4 | **Calendar-centric home**: left channel list, main view = one grid holding scheduled + drafts + published | Postiz docs ("The calendar is both the plan and the record") + review ("dashboard opens to a clean, calendar-centric layout") | Schedule page keeps its staggered-times card (per-platform best windows) and adds the Postiz **list view with state filters** (All / Scheduled / Awaiting approval / Published / Failed, with counts) — the calendar grid itself is not built; the list is the v0.3.0 scope. |
| 5 | **Failed post = red ring + error in place** | Postiz docs ("A red ring means it failed. The error is in the tooltip.") | Schedule list: failed posts get a red ring and their stored `error` + attempt count rendered inline (not just a tooltip — an error you have to hover to see is not diagnosable). Same treatment for failed jobs on the dashboard and Automation history. |
| 6 | **Per-channel editor / one draft, per-channel voices** | Postiz ("write a post once, tweak it per channel") | Already existed as per-platform SEO tabs in `ReviewClient`; v0.3.0 does not change it, only keeps it visible under the new pipeline card. |
| 7 | **Left sidebar lists channels; collapsible** | Postiz docs + v2 PR (collapsible sidebars) | Top nav gains Channels + Automation entries; the dashboard has a Channels overview strip and a dedicated Channels page (one card per platform with connection state + latest stats). A collapsible sidebar was **not** built — the existing top-nav layout was kept for scope. |
| 8 | **Properly designed dark mode, not a dark filter** | Postiz review ("Dark mode is properly designed") | The whole v0.3.0 pass: one token set in `components/ui.tsx` (neutral-950 page / neutral-900 cards / neutral-800 borders, amber-400 primary, emerald/sky/red/violet status colours), applied on every screen including loading/empty/error states. |
| 9 | **Settings: pick a provider, test the connection, save** | autoclip desktop setup ("select a model provider, test the connection, save") | Channels/Settings show per-platform connection state. **Update (v0.3.0 channels work):** Settings now has real credential fields (LLM key, per-platform client id/secret, masked to last 4 chars) saved via `PUT /settings`, and **Test connection** is now a real endpoint — `POST /settings/test-connection/{service}` hits the live API and reports the exact result. See "Channels / connections / API settings" below. |

## Channels / connections / API settings (v0.3.0)

Same rule as above — design borrowed, no code copied, no repo forked.
Sources re-checked live 2026-11-21; star counts are the research
report's 20 Nov 2026 snapshot.

### Borrowed (design idea → where it landed)

| Pattern | Source | Licence | How ClipForge uses it |
|---|---|---|---|
| OS-keyring-first secret storage: keys in the OS keyring (Credential Manager / Keychain / Secret Service), file fallback that **says so** in the UI and in diagnostics | artbyjazi/autoclip (MIT, ~470★) | MIT | `src/clipforge/auth/secrets.py` — keyring probe → Fernet-encrypted file fallback, active backend shown on the Settings page. Fresh implementation; no code copied. |
| Per-platform connection health check (token status + account info) and save-and-test API keys without restarting | freespirits/social-auto-engine (MIT, 31★) — "Connection health" + "Inline API key management" | MIT | `GET /channels` status cards and `POST /settings/test-connection/{service}` follow the same UX shape. Fresh code. |
| Token refresh helper: short-lived → long-lived exchange (Meta 60-day tokens) | freespirits/social-auto-engine (MIT) — `scripts/refresh_token.py` | MIT | `clipforge.auth.oauth.refresh_access_token` (Meta `fb_exchange_token` path). Fresh code. |
| Provider contract: one adapter per platform with `generateAuthUrl` / `authenticate` / `refreshToken` / `post`, plus error mapping to system types | gitroomhq/postiz-app (AGPL-3.0, 42k★) — **design reference only** | AGPL-3.0 — **do NOT fork** | `src/clipforge/auth/providers.py` + `oauth.py` mirror the contract shape (authorize-URL builder, code exchange, refresh, profile fetch, provider-error → system-error mapping). No Postiz code is used, read, or copied. |
| Scheduler / analytics design: provider adapters, per-account caps, idempotency, status machine | gitroomhq/postiz-app (AGPL-3.0) / inovector/mixpost (MIT Lite) — **design reference only** | AGPL-3.0 / MIT Lite | The daily-cap table (`publishing/limits.py`) feeds the per-channel caps on the Channels page. Mixpost Lite publishes only to Facebook Pages / X / Mastodon and is stale since Mar 2026 — data-model inspiration only, no code. |
| Manual-post pack as the honest fallback while platform review is pending | zhouxiaoka/autoclip (MIT, 11.8k★) — publish pack is manual | MIT | The Channels page shows the manual-post pack / inbox-draft fallback per platform (`providers.py` `fallback` field, `docs/channels.md`). |
| Resumable-upload + dry-run pattern (YouTube) | padderzahoor/youtube-shorts-scheduler (MIT, 1★) | MIT | Already in the YouTube adapter (v0.1.0); unchanged by channels work. |

### Built fresh (no reuse)

- OAuth flow (`clipforge/auth/oauth.py`) — authorize URL, code exchange, refresh, profile fetch, state store, error mapping. Platform specifics (Google offline access, Meta long-lived exchange, TikTok comma scopes) and the loopback-redirect desktop shape; no evaluated repo ships this exact flow as a reusable module.
- Encrypted token/settings stores (`token_store.py`, `settings_store.py`, `channels.py`) — ClipForge's own data model (SocialAccount row + encrypted blob), masked views, dry-run override semantics.
- Channels + API Settings pages (`apps/web/app/channels/`, `apps/web/app/settings/`) — ClipForge's Next.js static-export UI and API client; the env-var-model Channels page was upgraded to the OAuth-backed `/channels` endpoint.
- Test-connection endpoints — ClipForge-specific auth checks per service (YouTube `channels.list`, Meta `/me`, TikTok user info, LLM `/models`).

### Explicitly NOT reused (channels area)

- **gitroomhq/postiz-app** (AGPL-3.0): reference / self-host only. Do not fork for SaaS.
- **makiisthenes/TiktokAutoUploader (autotok)** (AGPL-3.0 + browser-login upload, not the official Content Posting API): do not reuse in product — study only.
- **linouk23/youtube_uploader_selenium, ContentAutomation/YouTubeUploader** (Selenium / cookie automation): do not use — ToS-risky, stale.
- **webskill01/insta-reel-bot** (reposts other creators' Shorts): do not copy — copyright / reused-content risk.
- **satyamsen7/Reels-Shorts-Scheduler** (no licence): do not reuse — reference only.

### Licence credits (channels area)

- artbyjazi/autoclip — MIT. Design: OS-keyring secret storage.
- freespirits/social-auto-engine — MIT. Design: approval queue, connection health, token refresh helper.
- gitroomhq/postiz-app — AGPL-3.0. Design reference only (provider contract, scheduler/analytics); no code used.
- inovector/mixpost — MIT (Lite). Design reference only (data model).
- zhouxiaoka/autoclip — MIT. Design: manual-post publish pack fallback.
- padderzahoor/youtube-shorts-scheduler — MIT. Pattern: resumable upload + dry-run.

All ClipForge code in this area is original work under the repo's MIT
licence. Before any future fork, verify the licence file in the pinned
commit — star counts and licences drift.

## Built fresh (no external pattern)

- **Live stats cards that degrade honestly**: analytics-driven cards with a
  "not connected yet" empty state (no invented numbers); demo fallback is
  always badged.
- **Monetization vs thresholds with the Feb 2027 doubling flag**, computed
  from real metrics passed to `GET /monetization` (the bare endpoint
  defaults to 0 — the frontend never shows that).
- **Automation view**: runner state (running / queued / history / next),
  retention-signal feed from `GET /analytics`, "Run scheduler now"
  (`POST /scheduler/run`, dry-run default), and local-only rule toggles
  with a locked dry-run guard.
- **Job retry via re-create** (`POST /jobs` with the same source) with
  demo-mode simulation; failed jobs deliberately mark **no** stage as
  failed because the backend reports no failed-stage field.
- **Design-system primitives** (`Card`, `StatusPill`, `ProgressBar`,
  `Skeleton`, `EmptyState`, `ErrorBanner`, `Toggle`, `PrimaryButton`) in
  `apps/web/components/ui.tsx`.

## Deliberately NOT borrowed

- **Postiz code / fork** (AGPL-3.0): design reference only, per research §9.
- **TikTokAutoUploader / Selenium uploaders**: unofficial upload paths are
  a ToS risk (research §9c); ClipForge uses only the official Content
  Posting API and stays in inbox-draft mode until the audit passes.
- **OpusClip-style black-box virality score**: ClipForge keeps score +
  reason transparent and editable (research §1 gap).
- **AutoClip's publish pack as manual ZIP**: ClipForge's approval queue +
  scheduler is the delivery path instead.

## v0.10.0 — ClipNova-AI (MIT) credit

Source: https://github.com/ahsanK225/ClipNova-AI — MIT licence
(checked 2026-11-23; repo is MIT-licensed, so adaptation with credit is
permitted). ClipNova was studied as the reference implementation for the
four v0.10.0 features; **no ClipNova code was copied or vendored** — every
feature below is a fresh implementation in ClipForge's own codebase, in
ClipForge's own architecture (FastAPI server + Next.js UI + the existing
pipeline/worker). What was adapted, per feature:

| Feature | ClipNova idea adapted | Where it landed in ClipForge |
|---|---|---|
| Live stream monitoring | `studio/live.py`: live recording + chat-spike peak detection (`_watch_spikes`, anonymous Twitch IRC `justinfan`, golden-retention clip window around a peak) | `src/clipforge/live_monitor.py` (PeakDetector, YouTube-first live monitor, Twitch/Kick honest limits) + `apps/api/clipforge_api/live_monitor.py` (`/api/live/*`) + `apps/web/app/live/page.tsx`; details in `docs/live-monitoring.md` |
| Facecam Studio | `render.py` layout modes: `split_half` / `custom` split, `gameplay` (camera over gameplay), `zoom`, `blur_fill` (16:9 source over a blurred background), `fullscreen` | `src/clipforge/facecam.py` (FacecamLayout: split / pip / fullscreen / blur_fill, ffprobe-verified 1080x1920 render) + `src/clipforge/render.py` facecam path + Studio page; ClipNova's facecam-box auto-detection from the video itself was NOT ported (ClipForge uses explicit user-set layout instead) |
| Animated captions | ClipNova's captioned-clip rendering (word-level animated captions on vertical video) | `src/clipforge/captions.py`: 6 animated caption styles, rendered by the real render path (`tests/unit/test_render_facecam.py`) |
| CapCut draft export | ClipNova's use of the public `pyJianYingDraft` library (MIT) to write CapCut draft folders | `src/clipforge/capcut.py` — ClipForge's own draft builder (own `draft_content.json` writer, no pyJianYingDraft dependency vendored); validated with `validate_draft`; details in `docs/capcut-export.md` |
| LLM providers | `studio/ai.py` `PROVIDERS` table shape (one entry per provider: key env var, base URL, model list) + model-id validation on save | `src/clipforge/llm.py`: 8 providers (Gemini, OpenAI, Grok, Groq, OpenRouter, Claude, Ollama + rules fallback) with per-provider Test buttons in Settings (`apps/web/components/LlmProviders.tsx`); details in `docs/ai-providers.md` |

### SKIPPED: sherpa-onnx (offline transcription)

ClipNova uses **sherpa-onnx** for offline transcription. Deliberately NOT
adopted in v0.10.0. Reason, stated honestly: ClipForge already has
working offline transcription via **faster-whisper** (with a memory guard
added in v0.9.3), which covers the same job. Adding sherpa-onnx would pull
in a second heavy native dependency (ONNX Runtime + model files) for
marginal gain — a second transcription engine to install, ship, and keep
working on Windows for no user-visible improvement. If faster-whisper ever
stops meeting ClipForge's needs (e.g. a model faster-whisper cannot run),
revisit sherpa-onnx then; until that day it stays out.
