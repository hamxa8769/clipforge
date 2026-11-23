# ClipForge

**AI short-form clipping + per-platform SEO + approval-first publishing for
YouTube Shorts, Facebook Reels, Instagram Reels, and TikTok.**

ClipForge turns one long video you own into scored 9:16 clips with burned-in
captions, writes a different title / description / hashtag set for each
platform, and publishes **only after a human approves** — through the
platforms' official APIs, and nothing else.

> **Status: v0.9.0 — production SaaS rebuild.** The clipping
> pipeline, review UI, and publishing adapters are under active
> development. Nothing in this repo posts to any platform automatically,
> and `DRY_RUN=true` is the default. v0.9.0 is the dashboard-first
> production rebuild: channels stay connected (proactive token refresh
> + health monitor), a real job queue (one job at a time, pause /
> resume / cancel, auto-run on creation), a notifications centre with
> plain-language errors, a full audit that fixed every dead button,
> and a modern SaaS UI. v0.8.2 fixes the app never opening
> (splash said the server did not answer `/health` within 30 s while
> `/yt-dlp/status` answered fine): `/health` no longer touches the
> keyring, and startup waits up to 120 s for a slow server instead of
> killing it. v0.8.1 fixes the server staying
> alive after the app closes (close = quit, no background server), the
> NSIS installer hitting a locked `clipforge-server.exe`, the Agent
> on/off toggle, and adds one-click yt-dlp install.
> v0.8.0 adds ClipForge Pro: Clip
> Studio with deeper AI detection, Export Hub, Batch mode, X (Twitter),
> a monetization layer, and low-end / no-GPU performance profiles.
> v0.7.0 adds Creative Commons
> discovery (Licensed only toggle), licence-checked CC clipping with
> auto-attribution and generated thumbnails, and live-stream clipping.
> v0.6.0 adds live niche trending
> search with a trending-to-agent loop, and a Growth view that turns your
> own posted-clip stats into advice. v0.5.0 adds a live dashboard, a
> per-job pipeline view, the ClipForge Agent (approval-first auto-clipping
> with an optional Gemini AI engine), and a five-step guided setup. v0.4.2
> fixes the two things users actually hit in v0.4.1: links inside the desktop app did nothing at
> all (no Tauri capability granted the opener, so every external link
> was silently denied), and the FFmpeg installer could report success
> on a bad or truncated download. v0.4.1 fixes the two things users
> actually hit in v0.4.0: "Connect a channel" no longer shows raw API
> JSON (the server now serves the wizard page for browser navigations),
> and FFmpeg installs itself with one click instead of asking for a
> manual download. v0.4.0 adds the guided Connect
> wizard (no raw JSON anywhere — step-by-step per platform, with the
> exact developer-console steps, a copyable redirect URI, masked keys,
> and a real Test Connection), a first-run onboarding wizard, the
> in-app updater (check for updates in the app — no more manual
> downloads; your clips, settings and keys are kept), and one window
> only (server status + logs live in the main window's status bar and
> log drawer — the separate server-status window is gone). v0.3.0 made
> the app one live, fully working flow: a visible ingest → transcribe
> → score → render → SEO → review → approve → schedule → publish
> pipeline with per-stage logs and retry, server-side automation
> rules, real channel connections (OAuth, encrypted tokens) and API
> settings. v0.2.1 fixes the desktop app showing as a plain unstyled
> page in v0.2.0 (the stylesheet was never bundled) — the app now
> shows its real dark theme. v0.2.0 added the single double-click
> desktop app (Windows installer) with the server built in — no
> terminal, no Node, no separate server window.

## Connecting a channel (guided — no JSON needed)

Open the app → **Channels → Connect a channel**. The wizard walks you
through it in five steps, about five minutes per platform:

1. **Pick a platform** — YouTube, Instagram, Facebook, or TikTok.
2. **Create the app** — the wizard shows the exact developer-console
   link and click-by-click steps (create a project, enable the API,
   create an OAuth client, add yourself as a test user). The redirect
   URI is shown with a Copy button — paste it exactly as shown.
3. **Paste the keys** — Client ID and Client Secret go into masked
   fields. They are stored encrypted on your machine (OS keyring first,
   encrypted file fallback) and never shown back in full.
4. **Connect** — one big button opens the platform's own sign-in page
   in your browser. ClipForge never asks for your platform password.
5. **Test** — Test Connection actually calls the platform and shows
   your connected account name, or the exact error in plain words
   with what to do next.

Until a channel is connected, the dashboard honestly says "No channel
connected yet" — no fake stats. Notes: YouTube needs Google verification
for public use (test users work meanwhile); Meta needs App Review +
Business Verification; TikTok needs a Content Posting API audit and
stays in inbox-draft mode until it passes.

**First run:** a 4-step onboarding wizard checks FFmpeg (one click
installs it if it is missing — clipping cannot work without it), offers
an optional LLM key, connects your first channel, and runs a first clip.
It appears once; replay it from Settings → Replay onboarding.

## Updating the app (in-app — no manual downloads)

In the desktop app, **Settings → Updates** (or the "Update available"
banner) checks GitHub for the latest release. When a newer version
exists it shows the version and size; press **Download & install** and
the app downloads it, verifies the size, installs it, and restarts.
Your clips, settings, and keys are kept — the update never wipes your
data folder. Every release gets a version bump, a tag, and a release
on GitHub; this is the rule from v0.4.0 onward.

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

**Download `ClipForge-0.3.0-setup.exe` (or the `.msi`) from
[GitHub Releases](https://github.com/hamxa8769/clipforge/releases/tag/v0.3.0),
double-click it, done.** The app starts its own built-in server
automatically — there is no terminal window, no Node.js install, and no
separate server to start. Logs and data live in
`%APPDATA%\com.clipforge.app\` (server log: `logs\clipforge-server.log`).

FFmpeg is installed in one click from inside the app — press
**Install FFmpeg** in the onboarding wizard, the banner, or
Settings → This app. It downloads the official build (Windows: gyan.dev
essentials, linked from ffmpeg.org; Linux: johnvansickle static build),
installs it into the app's data folder, and adds it to your Windows
PATH. No manual download, no restart. Without FFmpeg the app still runs
(review / SEO only) and tells you so. From the command line:
`python scripts/install-ffmpeg.py`.

## Download the executable (no Python needed)

Every release ships a ready-to-run zip — no Python install required:

- [GitHub Releases](https://github.com/hamxa8769/clipforge/releases/tag/v0.3.0)
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

- **FFmpeg is installed in one click** (onboarding, banner, or
  Settings → This app) — the exe does not bundle it, the app installs
  the official build itself (see `GET /ffmpeg/status` /
  `POST /ffmpeg/install` in the API table below).
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

## What's new in v0.9.0 — production SaaS rebuild

- **Dashboard-first workflow.** The daily flow happens on the
  Dashboard without tab-hopping: New clip run (licence check, then
  clip or run the agent), live jobs with Pause / Resume / Cancel /
  Retry, the approval queue inline, Needs attention, connected
  channels with honest status, recent activity and Up next.
- **Channels stay connected.** Root causes fixed: no proactive
  refresh (YouTube tokens live 1 h), Meta never exchanged its
  short-lived token, X refresh used the wrong auth, reconnect could
  destroy the refresh token, and disconnects were silent. Tokens now
  refresh 15 min before expiry (3 attempts, backoff), Meta gets a
  60-day long-lived token, X refresh uses HTTP Basic auth, and a
  disconnect produces a notification + a reason and next step instead
  of silently dropping the channel.
- **Real job queue + auto-run.** One active job at a time — a second
  job queues honestly as "Queued — position N" and never collides.
  Jobs created from the dashboard start automatically (they used to
  sit `pending` forever with nothing to run them); Pause / Resume /
  Cancel work per job (cancel kills the ffmpeg / yt-dlp process);
  `POST /jobs/{id}/run` is idempotent, and `/run` on a failed job no
  longer 409s (it resets and re-runs). Restart-safe: interrupted jobs
  re-queue, nothing runs twice.
- **Proper errors + notifications.** Every failure is plain language
  (what failed, why, what to do next) — no tracebacks, no raw JSON.
  A notifications centre (header bell, unread count, persistent
  list) is wired into every failure path.
- **Full audit, no dummy controls.** ~108 controls audited: Batch
  Mode fixed (it 422'd on every start), Approval Queue no longer
  lies about approving, Export Hub's scan/publish ID mix-up fixed,
  Trending 404 links fixed, Connect wizard's false "Connected"
  badge fixed. Anything that cannot work is honestly disabled with
  the reason. See `docs/AUDIT-v0.9.0.md`.
- **Modern SaaS UI.** New design system (dark-first, 150 ms
  transitions, Low-performance mode disables animations), consistent
  loading / empty / error states on every page. Light theme is
  deliberately not included — a half-done light theme looks worse
  than dark-only.
- **Web fixes from the audit:** `startBatch` now sends plain string
  sources (+ rights declaration) instead of objects, and all four
  request helpers surface FastAPI validation messages instead of a
  generic "Request failed (422)".

## What's new in v0.8.2 — app opens again: startup health fix

- **`/health` never touches the keyring.** Every `/health` call used to
  build a fresh `SecretStore`, whose init probes the OS keyring (a
  Credential Manager read on Windows). A slow or hung Credential
  Manager hung `/health` — the endpoint the desktop polls to decide
  the server is up — while `/yt-dlp/status`, which never touches
  secrets, answered fine. `/health` is now a pure liveness probe
  (same 4 fields), `dry_run` is cached at startup, and the secret /
  settings stores are cached per data dir with a lock-guarded Fernet.
- **Startup waits up to 120 s, and never kills a slow-but-healthy
  server.** The splash used to give up at 30 s and kill the server —
  a frozen-exe cold start can take longer, so Retry repeated the same
  doomed cycle forever. Now a server that exited fails fast with its
  exit code and the bind error / traceback from the log, a server that
  is still starting keeps being waited on (elapsed time shown), and a
  timed-out but alive server is reused by Retry instead of restarted.
  The splash log is rotated per launch, timestamped, and shows only
  the current run's lines; stale cleanup also acts when a port accepts
  TCP but `/health` hangs.
- **Working "Server status & settings" panel.** The dead button now
  opens an inline panel (live status, port setting, data folder) in
  the same window — no second window.
- **Every external link opens in the browser.** The last two raw
  `<a target="_blank">` links (Trending licence details, Review
  source video) now go through `openExternal()` / `ExternalLink`,
  so the Rust `open_external_links` test is 4/4.
- **CI now runs the Rust tests.** A new `rust` job on
  `windows-latest` runs `cargo test`, and the release workflow runs
  it too on the exact release code — the desktop tests never ran in
  CI before this release.

## What's new in v0.8.1 — shutdown fix, Agent toggle, one-click yt-dlp

- **Close = quit.** Closing the main window now stops the
  `clipforge-server` sidecar and exits the app completely — nothing
  keeps running in the background, so the server can never lock its
  own exe and block the next install. The sidecar is killed as a
  whole process tree (Windows Job Object with kill-on-job-close,
  Unix process group), stale servers from previous runs are cleaned
  up on startup, and the in-app updater stops the server and waits
  for the process and the exe file to be free before launching the
  installer. The Windows installer also kills leftover ClipForge
  processes before writing any file.
- **Agent on/off toggle fixed.** The toggle now really starts and
  stops the agent and persists across restarts; turning it on fails
  fast with a plain message when a required tool is missing instead
  of silently doing nothing.
- **One-click yt-dlp install.** A missing yt-dlp no longer blocks
  the Agent at Ingest with only an error: an "Install yt-dlp" button
  sits right on the blocked Ingest stage (and on the Agent and
  Settings pages), downloads the official release, verifies it, and
  installs it into the app data dir. See `docs/missing-tools.md` for
  the full audit of every other tool the pipeline needs.

## What's new in v0.8.0 — ClipForge Pro

- **Clip Studio** — AI clip suggestions as cards with a viral score
  (0–100) and six signal bars (hook, peak, payoff, emotion, visual,
  completeness), inline preview, a draggable timeline with trim and
  ripple cuts (with undo), and a labelled Pro Mode (caption styling,
  punch-in zooms, aspect 9:16 / 1:1 / 16:9). Simple by default, pro
  controls out of the way.
- **Deeper AI clip detection** — Whisper-style word-level transcripts
  are segmented into sentences/topics, cut into sentence-anchored
  15–60 s windows, and scored on six signals with a documented
  weighted formula (see `docs/detection.md`):
  `viral = 0.25*hook + 0.25*peak + 0.15*payoff + 0.10*emotion +
  0.10*visual + 0.15*completeness`. Near-duplicates are removed
  (time IoU > 0.5) and topic diversity is enforced, so the top 8–12
  clips are not the same moment twice. Hook titles come from Gemini
  when connected, rule-based otherwise.
- **Export Hub** — one-click publish with platform rules enforced by
  the app: YouTube Shorts ≤ 60 s, TikTok ≤ 60 s, Instagram Reels
  ≤ 90 s, X video ≤ 140 s; auto aspect ratios, captions, hashtags,
  and a thumbnail preview side-by-side with the clip. Cross-post
  scheduling uses the Growth page's own best-time data.
- **Batch mode** — drop up to 20 videos, queue them, and wake up to
  clips ready: per-video progress, pause/resume, and the real
  pipeline runs each item (not a simulation). YouTube URLs need an
  own-content declaration — the guardrail still applies.
- **X (Twitter)** — fifth platform in Channels: OAuth 2.0 + PKCE,
  video upload and posting, daily cap 25. Honest in the UI: posting
  needs an X API Basic plan (paid) or higher — the free tier is
  read-only, so until a paid tier is active X clips stay as drafts /
  manual-post packs.
- **Monetization layer** — affiliate-link inserter for descriptions,
  UTM tracking per clip, A/B titles + thumbnails with view tracking,
  and a revenue dashboard (RPM/views/earnings where the platform API
  exposes them; honest empty states where it does not). A copyright
  scan runs before publish — metadata-only flags with a plain-language
  warning, never a guarantee.
- **Low-end / no-GPU performance mode** — the app measures the
  machine (CPU cores, RAM, GPU) on first run and picks Low / Balanced
  / High: Low uses Whisper tiny, 480p previews, CPU-only FFmpeg, 1
  job at a time and reduced animations. The active profile and the
  hardware behind it are shown in Settings, and the user can override
  it. Long jobs queue and progress — the UI never blocks.
- If you have v0.7.0 installed, update from inside the app
  (Settings → Updates, or the "Update available" banner) — no need to
  download anything by hand. Installing v0.8.0 over v0.7.0 keeps your
  clips, settings, and keys.

## What's new in v0.7.0

- **Creative Commons discovery** — the Trending page now has a prominent
  "Licensed only (CC)" toggle (default ON, your last choice is
  remembered on this device, never forced on). When ON, search uses
  YouTube's `videoLicense=creativeCommon` filter and every result is
  verified (`status.license == "creativeCommon"`) before it is shown,
  with a licence badge on each video. When OFF, trending shows all
  videos as before — but each result still says clearly whether it can
  be clipped, and standard-licence videos cannot be clipped.
- **Licence check** — paste any YouTube URL on the Trending page and the
  app checks its licence live: Creative Commons / public-domain / your
  own video → "Can clip"; standard-licence video by another creator →
  "Can't clip", with a plain-language reason. The agent and the publish
  path refuse standard-licence third-party videos (422/409).
- **CC clipping pipeline** — pick a CC video → transcribe → score →
  clip (9:16, captions) → light edit → NEW thumbnail (generated from
  the clip, with Regenerate and 2 variants) → SEO → approval queue →
  schedule/post. For CC-BY sources the description automatically
  carries the required credit (original title, creator, source URL,
  licence name + link) — built into SEO so it is never forgotten and
  never duplicated. Every clip record keeps its source licence info.
- **Live video clipping** — the "Live now" tab lists live streams with
  viewers, start time and licence. Capture a bounded segment
  (60–300 s) from your OWN live stream or a CC-licensed live stream,
  then the normal clip → SEO → approve → post flow runs. If a live
  stream cannot be captured (ended, DRM, age/geo restricted), the job
  fails with a plain-language reason — nothing is faked.
- If you have v0.6.0 installed, update from inside the app
  (Settings → Updates, or the "Update available" banner) — no need to
  download anything by hand. Installing v0.7.0 over v0.6.0 keeps your
  clips, settings, and keys.

## What's new in v0.6.0

- **Trending** — live YouTube trending search per niche (views/hour
  velocity + engagement, ranked by a documented trend score). Works with
  a connected YouTube channel or a saved YouTube Data API key; with
  neither, it says so plainly instead of inventing videos.
- **Trending → Agent loop** — a trending video is the topic only. The
  agent matches it to your own source video on that topic, runs the
  normal pipeline with SEO seeded from the trending title, and queues
  posts approval-first. A trending video URL is refused as a clip
  source — the agent clips only your own videos.
- **Growth view** — monitors your published posts, fetches current
  YouTube stats, and turns the real numbers into advice: best posting
  hours, best titles, niche ranking, what to post next. Every
  recommendation is labelled with its basis ("from your N posted clips"
  once 5+ clips have stats, otherwise YouTube best practice); no
  performance data is ever invented. TikTok / Instagram / Facebook stats
  are honestly "unsupported" for now.
- **Web fix** — browser navigations on UI-shaped paths with no page now
  redirect instead of showing raw API JSON.
- If you have v0.5.0 installed, update from inside the app
  (Settings → Updates, or the "Update available" banner) — no need to
  download anything by hand. Installing v0.6.0 over v0.5.0 keeps your
  clips, settings, and keys.

## What's new in v0.5.0

- **Live dashboard** — the app now opens on a real desktop shell (fixed
  sidebar, status bar) with live server / FFmpeg / dry-run / automation
  health, connected accounts with followers and automation counts, and
  honest "not connected yet" states. Nothing is demo data unless it is
  labelled demo.
- **Pipeline view** — every job's full process, live: Source → Transcribe
  → Score → Clip review → SEO → Approve → Schedule → Publish, with real
  per-stage status, progress, and stage logs from the server.
- **ClipForge Agent** — the agent watches one video URL or a whole
  YouTube channel, makes clips, and queues them for approval. Approval-first
  is the default: nothing posts without your approval unless you turn
  auto-post on. With no AI key it uses the built-in rule-based engine; with
  a Gemini key it picks smarter moments and writes titles.
- **Gemini AI engine (optional)** — paste a Google AI Studio key in
  Settings (or the setup wizard), Save, then Test connection. The key is
  stored encrypted on the server and never shown again. The Test button
  now answers in plain language even on a broken machine proxy config
  instead of a 500 error.
- **Guided setup wizard** — five steps on first run (FFmpeg → Gemini key
  → Connect a channel → Niche & source → Turn on the agent), resumable,
  skippable, and available again from Settings. Also as a full page at
  `/setup`.
- **No more raw JSON for live jobs** — the Pipeline "Open job" link now
  goes to the review page, and a direct load / refresh of `/jobs/<live-id>`
  redirects to it instead of showing the API's raw JSON.
- If you have v0.4.2 installed, update from inside the app
  (Settings → Updates, or the "Update available" banner) — no need to
  download anything by hand. Installing v0.5.0 over v0.4.2 keeps your
  clips, settings, and keys.

## What's new in v0.4.2

- **Links inside the desktop app now open in your browser** — every
  external link (Google Cloud Console, the API Library, Credentials,
  the download link in the FFmpeg installer, the update download) now
  goes through the Tauri opener plugin and opens in the system browser.
  Root cause of the v0.4.1 bug: no Tauri capability file existed, so
  every opener invoke from the loopback-served UI was denied by the
  ACL, silently — `window.open` was never intercepted either. The app
  now ships `capabilities/default.json` granting the opener (and the
  other commands the UI actually uses), an `open_external_url` command
  that only allows plain `http(s)` URLs, and an `ExternalLink`
  component used by every external anchor.
- **FFmpeg installer is hardened** — the installer now checks the
  checksum against the final URL after redirects, verifies the
  download size and the zip magic bytes, retries once and then tries
  the official fallback sources, handles flat zips (no top-level
  folder), and reports the failed step plus a PATH warning instead of
  a silent success. A Windows PATH failure no longer claims a clean
  install. The installer UI shows the warning and refreshes its
  state after a successful install.
- If you have v0.4.1 installed, update from inside the app
  (Settings → Updates, or the "Update available" banner) — no need to
  download anything by hand. Installing v0.4.2 over v0.4.1 keeps your
  clips, settings, and keys.

## What's new in v0.4.1

- **"Connect a channel" no longer shows raw JSON** — a full-page
  navigation to `/channels/connect` (and `/channels`, `/settings`,
  `/jobs/view`, `/monetization`) used to be claimed by the API routes
  and answered with `{"detail": "unknown platform: connect"}`. The
  server now serves the exported wizard page for real browser
  navigations, while API fetches still get JSON. In-app buttons are
  also real client-side links now, and `?platform=youtube` starts the
  wizard at step 2 instead of asking for the platform a second time.
- **One-click FFmpeg install** — press **Install FFmpeg** in the
  onboarding wizard, the missing-FFmpeg banner, or Settings → This app.
  The app downloads the official build (Windows: gyan.dev essentials
  zip linked from ffmpeg.org; Linux: johnvansickle static build),
  checks the published SHA-256 when one exists, installs it into the
  data folder, verifies `ffmpeg -version`, saves the path, and adds it
  to the Windows user PATH — with download progress, plain-language
  errors that name the failed step (download / verify / extract /
  install), and retry. A failed download is retried once, then the
  installer tries the fallback sources in order (the direct gyan.dev
  packages URL, then the BtbN build, both linked from ffmpeg.org);
  a Windows PATH failure is shown as a warning instead of silently
  reporting a clean install. Clipping works right after, no restart,
  no manual download. Same installer from the command line:
  `python scripts/install-ffmpeg.py`. macOS is not auto-installed yet
  (`brew install ffmpeg` instead). A **Reinstall FFmpeg** button in
  Settings / onboarding re-installs over an existing FFmpeg.
- **One-click yt-dlp install** — the same fix for the other missing
  tool: press **Install yt-dlp** right on a blocked Ingest stage
  (Pipeline), on the Agent page when a run cannot start, or in
  Settings → This app. The app downloads the official release binary
  (Windows: `yt-dlp.exe`; Linux: `yt-dlp_linux`; macOS: `yt-dlp_macos`,
  all from github.com/yt-dlp/yt-dlp releases), checks it against the
  release's published `SHA2-256SUMS` SHA-256 when reachable, verifies
  `yt-dlp --version` runs, installs it into the data folder, saves the
  path, and adds it to the Windows user PATH — with download progress,
  plain-language errors that name the failed step (download / verify /
  install), and retry. A failed download is retried once, then the
  installer tries the version-pinned fallback release (2026.10.26,
  verified at build time). After the install, retry the blocked job
  from the same page and it runs past Ingest. Same installer from the
  command line: `python scripts/install-ytdlp.py`. See
  [docs/missing-tools.md](docs/missing-tools.md) for the audit of every
  other tool the pipeline needs.
- **Workflow fixes from a full audit** — local files given as
  `file://` URLs now ingest instead of failing with "source file does
  not exist"; the approval queue stays in sync after a live post; the
  job review page explains itself when a job finishes with zero
  candidates and shows real server progress; the schedule page no
  longer calls unscheduled posts "scheduled".
- If you have v0.4.0 installed, update from inside the app
  (Settings → Updates, or the "Update available" banner) — no need to
  download anything by hand. Installing v0.4.1 over v0.4.0 keeps your
  clips, settings, and keys.

## What's new in v0.4.0

- **Guided Connect wizard** — Channels → Connect a channel walks
  through five steps per platform (pick → create the developer app
  with exact links and click-by-click steps → paste masked keys →
  Connect → Test Connection with the account name or a plain-language
  error). The raw settings JSON is no longer the default view anywhere;
  it lives behind an "Advanced" toggle on the Settings page.
- **First-run onboarding** — a 4-step wizard (FFmpeg check with
  install help, optional LLM key, first channel, first clip) that
  appears once and is replayable from Settings.
- **In-app updater** — Settings → Updates (or the "Update available"
  banner) checks GitHub, downloads and installs the new version, and
  restarts. Clips, settings, and keys are kept. No more manual
  downloads from v0.4.0 onward.
- **One window only** — the separate "Server status & settings"
  window is gone. Server status (running, port, FFmpeg, dry-run) is a
  status bar in the main window, and logs live in an in-app drawer.
- **Honest FFmpeg handling** — jobs that need FFmpeg now fail with a
  clear "FFmpeg is missing — install it from ffmpeg.org" message
  instead of failing silently; the API reports FFmpeg found/path/
  version in `GET /settings`.
- If you have v0.3.0 installed, just install v0.4.0 over it — no need
  to uninstall first.

## What's new in v0.3.0

- **Live workflow** — every job shows its real pipeline
  (ingest → transcribe → score → render → SEO → review) with stage
  history, progress, ETA, per-stage logs (`GET /jobs/{id}/logs`) and
  the failed stage + run duration on `GET /jobs/{id}`. A failed job
  retries in place (`POST /jobs/{id}/retry`) — same job, old candidates
  re-derived, history kept. See `docs/workflow.md` for the curl
  walkthrough.
- **Automation rules on the server** — `GET/PUT /automation/rules`
  (retention-feedback picking, auto-SEO, staggered scheduling,
  auto-queue, high-score auto-approve) with an audit feed
  (`GET /automation/feed`); the Automation page toggles now write to
  the server instead of the browser. Scheduler state is truthful
  (`GET /scheduler/status`).
- **Channels + API settings** — `GET /channels` is the canonical
  channel status (OAuth token state, expiry, scopes, daily caps,
  review/audit notice, manual-post-pack fallback); the Settings page
  saves LLM + per-platform OAuth credentials via `PUT /settings`
  (encrypted on the server, masked to the last 4 chars) and tests them
  with `POST /settings/test-connection/{service}`.
- **Caption fix** — burned-in captions were broken since v0.2.1: the
  ASS file glued every Dialogue event onto one line, so only the first
  0.24 s of captions rendered (the rest showed as literal
  `Dialogue: ...` text). One-line fix in `src/clipforge/captions.py`,
  covered by `tests/unit/test_captions.py`, verified on a live render.
- **Static-export serving fix** — `GET /channels/{platform}` used to
  claim the Next.js RSC payload `/channels/index.txt` and 404 it, so
  every Channels visit logged console 404s. The server now serves real
  files under `web/out` before API routing (see
  `tests/integration/test_web_serving.py`).
- If you have v0.2.1 installed, just install v0.3.0 over it — no need
  to uninstall first.

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
| `GET` | `/ffmpeg/status` | Pollable FFmpeg install state + live detection |
| `POST` | `/ffmpeg/install` | Start the one-click FFmpeg install (202; `?force=true` re-installs) |
| `GET` | `/yt-dlp/status` | Pollable yt-dlp install state + live detection |
| `POST` | `/yt-dlp/install` | Start the one-click yt-dlp install (202; `?force=true` re-installs) |

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
