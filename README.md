# ClipForge

**AI short-form clipping + per-platform SEO + approval-first publishing for
YouTube Shorts, Facebook Reels, Instagram Reels, and TikTok.**

ClipForge turns one long video you own into scored 9:16 clips with burned-in
captions, writes a different title / description / hashtag set for each
platform, and publishes **only after a human approves** — through the
platforms' official APIs, and nothing else.

> **Status: v0.2.1 — early alpha.** The clipping pipeline, review UI, and
> publishing adapters are under active development. Nothing in this repo
> posts to any platform automatically, and `DRY_RUN=true` is the default.
> v0.2.1 fixes the desktop app showing as a plain unstyled page in v0.2.0
> (the stylesheet was never bundled) — the app now shows its real dark
> theme. v0.2.0 added the single double-click desktop app (Windows
> installer) with the server built in — no terminal, no Node, no separate
> server window.

---

## What it is

- **Clipping engine** — Whisper transcription with word-level timestamps →
  LLM highlight detection (word indices, not timestamps) → virality score
  0–100 with hook rating + reason → dedupe of >50% overlapping candidates →
  face-tracked 9:16 reframe + burned captions → 1080×1920 H.264 MP4.
- **Per-platform SEO** — one master clip, four native variants: different
  title, description, hashtag count, length, and cover per platform.
- **Approval-first publishing** — every post sits in an approval queue.
  Nothing publishes without an explicit human approve. TikTok stays in
  inbox-draft mode until its Content Posting API audit is passed.
- **Monetization tracker** — subs / views / watch-time vs. each platform's
  thresholds, including the YouTube Partner Program doubling for new
  applicants from 1 February 2027. It shows thresholds, never promised
  earnings.

## What it is not

- Not a fully-automatic poster. Fully-automatic, no-review posting is
  against TikTok's audit rules and is a fast way to get accounts flagged
  everywhere else.
- Not a repost bot. It only processes content you own or have rights to.
  It never downloads other creators' videos, never re-uploads third-party
  content, and never strips or crops out another app's watermark.
- Not a Selenium / cookie uploader. See [the DO-NOT list](#do-not-list).

## Architecture

```
Upload / own YouTube link
        │
        ▼
┌─────────────┐   ┌──────────┐   ┌──────────────┐
│   FastAPI   │──▶│  SQLite/ │──▶│   Workers    │
│  apps/api   │   │ Postgres │   │  (pipeline)  │
└─────────────┘   └──────────┘   └──────┬───────┘
        │                               │
        ▼                               ▼
┌─────────────┐                  ingest → transcribe
│  Next.js    │                  (Whisper) → highlights
│  apps/web   │                  (LLM, word indices)
│ review +    │                  → reframe 9:16 → render
│ approvals   │                  (FFmpeg + captions)
└─────────────┘                          │
        │                                ▼
        ▼                         per-platform SEO
┌──────────────────────────────────────────────┐
│ Approval queue → Scheduler → Official APIs   │
│  YouTube Data API v3 · Meta Graph API        │
│  TikTok Content Posting API (audit-gated)    │
│  + manual-post pack fallback (file + copy)   │
└──────────────────────────────────────────────┘
```

Full detail: [ARCHITECTURE.md](ARCHITECTURE.md).

## Quickstart

```bash
git clone https://github.com/hamxa8769/clipforge.git
cd clipforge

# Python 3.12, FFmpeg 8.1+ required. Node 24 for apps/web.
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env   # fill in only what you need; dry-run works with none

# API (http://localhost:8000)
cd apps/api && PYTHONPATH=src uvicorn clipforge_api.app:app --reload --port 8000

# Web review UI (http://localhost:3000)
cd apps/web && npm ci && npm run dev

# Smoke test: builds a 1080x1920 sample, runs the pipeline, verifies output
python3 scripts/smoke_test.py

# Tests + lint
python3 -m pytest tests/ -q
ruff check . && ruff format --check .
```

## Download the desktop app (Windows — no terminal, no Node)

**Download `ClipForge-0.2.1-setup.exe` (or the `.msi`) from
[GitHub Releases](https://github.com/hamxa8769/clipforge/releases/tag/v0.2.1),
double-click it, done.** The app starts its own built-in server
automatically — there is no terminal window, no Node.js install, and no
separate server to start. Logs and data live in
`%APPDATA%\com.clipforge.app\` (server log: `logs\clipforge-server.log`).

FFmpeg is still required separately for clipping — download it from
https://ffmpeg.org/download.html and put it on `PATH`. Without FFmpeg the
app still runs (review / SEO only) and tells you so.

## Download the executable (no Python needed)

Every release ships a ready-to-run zip — no Python install required:

- [GitHub Releases](https://github.com/hamxa8769/clipforge/releases/tag/v0.2.1)
  — `ClipForge-vX.Y.Z-Windows.zip` or `ClipForge-vX.Y.Z-Linux.zip`
  (published with each `v*` release; the asset appears on the release
  page once the release workflow has run).

Each zip contains:

| File | What it is |
|---|---|
| `clipforge-server` / `clipforge-server.exe` | One-file API server — runs without Python |
| `web/` | Web review UI (Next.js standalone build) |
| `start-server.bat` / `start-server.sh` | Start the API server |
| `start-web.bat` / `start-web.sh` | Start the web UI |
| `README.txt` | Setup instructions |
| `.env.example` | Copy to `.env` and fill in what you need |

```text
1. Unzip anywhere.
2. Start the API server:  double-click start-server.bat (Windows)
                          or ./start-server.sh (Linux)
   → API at http://127.0.0.1:8000 (docs at /docs).
3. Start the web UI:      double-click start-web.bat / ./start-web.sh
   → http://localhost:3000. Keep the API server running too.
```

Things to know:

- **FFmpeg is still required separately** (and on `PATH`) for clipping,
  transcription, and rendering — the exe does not bundle it.
- **The web UI needs Node.js 20+** to run (`start-web`). The API server
  does not. The Next.js app is server-rendered, so it cannot run inside
  the Python exe; if you place a static export of the web app at
  `web/out/` next to the exe, the exe *will* serve it at
  `http://127.0.0.1:8000/` instead of the API-only status page.
- First run creates a `data/` folder next to the exe with `clipforge.db`
  (SQLite) — your jobs, clips, and approvals live there.
- `DRY_RUN=true` is the default: nothing publishes live, and every post
  still waits for a human approve in the web UI.

Building the exe yourself: `python scripts/build_exe.py --with-web`
(see `clipforge-server.spec`).

## What's new in v0.2.1

- **Desktop UI styling fix** — v0.2.0 on Windows opened as a plain
  unstyled page (serif font, blue underlined links, white background)
  because the app layout never imported its stylesheet, so Tailwind CSS
  was never bundled into the static export. One-line fix
  (`import "./globals.css"` in `apps/web/app/layout.tsx`); the static
  export now ships a 14 KB stylesheet linked on every page, and the
  desktop app shows the intended dark theme with cards and navigation.
  Verified with screenshots of all five main pages.
- If you have v0.2.0 installed, just install v0.2.1 over it — no need to
  uninstall first.

## What's new in v0.2.0

- **Desktop app (Tauri v2)** — one double-click Windows installer
  (`.msi` + setup `.exe`). The app embeds the `clipforge-server` sidecar
  and the static web UI, starts the server itself, waits for `/health`,
  and shows the real dashboard — no terminal, no Node, no separate
  server window. Tray icon, server log viewer, settings (data folder,
  port, API key *names* only). Linux `.deb` / `.AppImage` are built in
  CI too when available. See `apps/desktop/README.md`.
- **Static web export** — `npm run build:static` produces plain files in
  `apps/web/out/`, served by `clipforge-server` at `/` with no Node at
  runtime (`python scripts/build_exe.py --with-web-static`). All pages
  fetch data in the browser; live jobs are reviewed at
  `/jobs/view/?id=<id>`, which works for any job id (the static
  `/jobs/<id>` pages only exist for the demo ids).
- **FFmpeg check** — the desktop app checks for FFmpeg on start and shows
  a download link instead of failing silently when it is missing.

## What's new in v0.1.1

- **Packaged executable** — `clipforge-server` one-file exe + Next.js web UI,
  built by GitHub Actions on Windows and Linux, attached to each release.
- **Video player + timeline trim editor** in clip review (draggable trim
  handles, preview-trim, numeric fallback); `pending`/`queued` status
  mismatch fixed; approve/reject now calls the real endpoint.
- **Bulk/CSV scheduling** (dry-run preview, per-row errors, idempotent
  re-import), **B-roll** (source-video / licensed-upload only, URLs
  rejected), **brand kits** (2 built-in templates), **A/B title variants**,
  and **retention analytics → next-clip feedback** (synthetic-data tested;
  live platform feeds still need API keys).
- Face tracking on a synthetic talking-head video did **not** engage
  (Haar found 0 faces; centre-crop fallback ran) — real-human tracking
  remains unverified. See `docs/face-tracking-test.md`.

## API endpoints

| Method | Path | What it does |
|---|---|---|
| `GET` | `/health` | Liveness + version + dry-run flag |
| `POST` | `/jobs` | Create a clipping job (own-content declaration required for YouTube) |
| `GET` | `/jobs/{id}` | Job status + candidate count |
| `GET` | `/jobs/{id}/candidates` | Candidates with scores + per-platform SEO |
| `POST` | `/jobs/{id}/candidates/{n}/approve` | Approve a candidate (reject/trim also exist) |
| `POST` | `/posts` | Create a post from an approved candidate (409 if not approved) |
| `POST` | `/posts/{id}/approve` | Approve a post |
| `POST` | `/posts/{id}/publish?dry_run=true` | Publish (409 if not approved; dry-run default) |
| `GET` | `/monetization` | Threshold progress per platform |
| `GET` | `/accounts` | Connected social accounts |

Full reference: [docs/api.md](docs/api.md).

## Approval-first + official-APIs-only policy

1. **Human approval is mandatory.** No clip is published, scheduled, or
   sent to any platform without an explicit approve action. There is no
   "auto-post everything" switch.
2. **Official APIs only.** YouTube Data API v3, Meta Graph API, TikTok
   Content Posting API. Each platform's OAuth scopes, app review, and
   audit gates are respected — including TikTok's rule that unaudited apps
   post private-only, and its inbox-draft mode while the audit is pending.
3. **Rate limits are read live, never hard-coded.** YouTube quota from
   Cloud Console, Instagram's `content_publishing_limit`, TikTok's
   `creator_info.max_video_post_duration_sec`. The numbers in
   [ARCHITECTURE.md](ARCHITECTURE.md) are starting defaults with
   `VERIFY_LIVE` notes where sources conflict.
4. **AI labelling is on by default.** AI-clipped / AI-voiced posts carry
   the platform's AI label (`is_aigc` on TikTok, YouTube disclosure, Meta
   AI-info signals). See [docs/compliance.md](docs/compliance.md).
5. **Own content only.** Ingest requires a source-ownership declaration.
   No third-party downloads, no reused-content pipelines.

## Reuse attribution

ClipForge is built fresh, but its clipping approach owes a debt to two MIT-
licensed open-source projects (ideas and prompt design reused, not code
copied wholesale):

- [zhouxiaoka/autoclip](https://github.com/zhouxiaoka/autoclip) (MIT) —
  one-link → up to 10 scored clips per platform, publish-pack concept.
- [Anil-matcha/AI-Youtube-Shorts-Generator](https://github.com/Anil-matcha/AI-Youtube-Shorts-Generator)
  (MIT) — Whisper → LLM virality framework (hook / emotional peak /
  opinion bomb / revelation / conflict / quotable / story / practical
  value) → score 0–100 → dedupe → face-tracked 9:16 render.

Other projects were studied as design references only; see
[ARCHITECTURE.md](ARCHITECTURE.md#reuse--references).

## DO-NOT list

These are never acceptable in ClipForge, in any PR, under any flag:

- ❌ **Postiz (AGPL-3.0) code** — reference / self-hosted scheduler design
  only. Forking it into a hosted service would force AGPL publication of
  the whole product.
- ❌ **Selenium / cookie / headless-browser uploaders** — unofficial upload
  paths get accounts flagged and break on platform changes.
- ❌ **Unofficial TikTok uploaders** (e.g. browser-login + web-API upload
  tools) — explicitly excluded by TikTok's audit; use the official
  Content Posting API or inbox drafts.
- ❌ **Third-party reposts** — no downloading other creators' videos, no
  re-uploading other people's content, no watermark cropping.
- ❌ **Templated AI-slop at scale** — repetitive / generic / mass-produced
  clips are demonetizable under YouTube's 16 July 2026 reused-content
  rules; ClipForge varies templates and requires human review.
- ❌ **Promising earnings or Reels Play bonuses** — Reels Play ended
  31 Aug 2025. The monetization tracker shows thresholds only.
- ❌ **Secrets in the repo** — no API keys, tokens, keystores, or
  `.env` files. Ever. See [SECURITY.md](SECURITY.md).

## Docs

- [docs/setup.md](docs/setup.md) — install, env vars, running locally
- [docs/api.md](docs/api.md) — API reference + job lifecycle
- [docs/publishing.md](docs/publishing.md) — per-platform OAuth / app-review /
  audit checklists
- [docs/monetization.md](docs/monetization.md) — thresholds per platform
- [docs/compliance.md](docs/compliance.md) — AI labelling, copyright, privacy

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Contract-first: the tests in
`tests/` define the module APIs; implement to the contract, keep
`DRY_RUN=true` the default, and never add a publishing path that bypasses
approval.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 hamxa8769.
