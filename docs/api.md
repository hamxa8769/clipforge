# API Reference — ClipForge v0.3.0

Base URL (local dev): `http://localhost:8000`. Interactive docs: `/docs`.
The app is `apps/api/clipforge_api/app.py` (imported as
`clipforge_api.app`); run it from `apps/api` with
`PYTHONPATH=src uvicorn clipforge_api.app:app --port 8000`.

Auth in v0.3.0: none on local dev (single-user). API-key auth is planned
before any shared deployment; do not expose the dev server publicly.

All responses are JSON. Publishing is **dry-run by default**: the
`dry_run` query parameter on publish defaults to `true`, and live
publishing is additionally blocked while `CLIPFORGE_DRY_RUN=true`
(the default). A dry-run publish returns the adapter's payload without
calling the platform.

## Job lifecycle

```
pending → transcribing → scoring → rendering → review → ready
                                                    ↘ failed
```

Candidates carry their own status: `pending → approved | rejected`.
Posts carry theirs: `pending_approval → approved → scheduled → published`
(or `dry_run` after a dry-run publish, `failed` after max attempts).
Trimming a candidate narrows its `start_s`/`end_s` within the original span.

- `review`: clips + scores are ready; a human picks / trims in the web UI.
- Approval is a hard gate twice over: a **post** can only be created from
  an `approved` candidate (otherwise 409), and a post can only be
  **published** once the post itself is approved (otherwise 409).

## Endpoints

### `GET /health`

```json
{ "status": "ok", "dry_run": true, "version": "0.3.0", "database_url_scheme": "sqlite" }
```

### `POST /jobs` → `201`

Create a clipping job (JSON body — no file upload in v0.1.0).

```json
{ "source_type": "local", "source_path": "/path/to/talk.mp4",
  "title": "Morning routine: test", "own_content_declaration": true }
```

- `source_type`: `local` or `youtube`.
- YouTube jobs **require** `own_content` / `own_content_declaration`
  = true, otherwise **422** — ClipForge only processes content you own
  or hold the rights to.
- Local jobs need `source_path` (or `source_url`), otherwise **422**.
- → `201` `{ "id": "...", "status": "pending", "own_content": true,
  "candidates": 0, ... }`.

### `GET /jobs` · `GET /jobs/{id}`

List jobs / one job (with candidate count). `404` for an unknown id.

### `GET /jobs/{id}/candidates`

Candidates with score / hook rating / reason / trim-aware start/end, plus
per-platform SEO variants (`seo.youtube|instagram|facebook|tiktok`, each
with `title`, `description`, `hashtags`).

### `POST /jobs/{id}/candidates/{n}/approve` → `200`

`{ "id": "...", "n": 1, "status": "approved" }`. Also
`.../reject` and `.../trim` (`{"start_s": ..., "end_s": ...}` — must lie
inside the candidate's span, otherwise 422).

> **Contract gap (frontend, 2026-11-20):** the candidate payload returns
> trim-aware `start_s`/`end_s` (i.e. the current trim, if any) but not the
> candidate's **original** span. The trim endpoint validates against the
> original span in the DB, so the web UI can only narrow within the span it
> was given — it cannot widen a previous trim back out. If widening is ever
> needed, return `orig_start_s`/`orig_end_s` (or `trim_start_s`/
> `trim_end_s` separately) from `GET /jobs/{id}/candidates`.

### `GET /media/clips/{job_id}/{filename}` · `GET /media/uploads/{filename}`

Read-only file serving for the web player's `<video>` tag: the rendered
9:16 clip (`data/clips/<job_id>/clip_01.mp4`, …) and source uploads
(`data/uploads/…`). Only `.mp4`/`.webm`/`.mov`, filenames only — no
sub-paths, no traversal outside the data dir. Unknown files are `404`.
The media root is `CLIPFORGE_DATA_DIR` (default `./data`).

### `POST /posts` → `201`

Create a post from an **approved** candidate (otherwise **409**).

```json
{ "clip_candidate_id": "...", "platform": "youtube",
  "video_path": "/path/to/clip_01.mp4", "scheduled_for": null }
```

`platform` must be `youtube|instagram|facebook|tiktok`. `seo` is optional
— if omitted it is generated from the candidate. New posts start in
`pending_approval`.

### `GET /posts?status=&platform=` · `GET /posts/{id}`

The approval queue (filterable), and one post (with `idempotency_key`,
`attempts`, `publish_result`).

### `POST /posts/{id}/approve` → `200`

Approves a `pending_approval`/`draft` post (otherwise 409). A post with
`scheduled_for` becomes `scheduled`, otherwise `approved`.

### `POST /posts/{id}/publish?dry_run=true` → `200`

Publishes an `approved`/`scheduled` post (otherwise **409** —
"Approval-first: approve this post before publishing it"). In dry-run
mode the adapter validates the payload and returns it; the post becomes
`dry_run`. With `dry_run=false` while `CLIPFORGE_DRY_RUN=true`, **409**.
A failed adapter call marks the post `failed` and returns **502**.

### `GET /monetization?platform=&subs=&...`

Threshold progress per platform (see [monetization.md](monetization.md)).
Shows thresholds only — never promised earnings.

### `GET /accounts` · `POST /accounts`

Connected social accounts. Tokens are stored as the **name** of an
environment variable only (`token_env_var`) — never a token value.
Legacy account-record endpoint, kept for compatibility: the canonical
channel-status endpoint is `GET /channels` below (the only one that
knows OAuth token state, expiry, scopes, daily caps, and the
review/audit notice).

### `GET /channels` · `GET /channels/{platform}`

**Canonical channel-status endpoint (v0.3.0).** One status card per
platform, from the real backend: `status` (`connected` | `expired` |
`not_connected`), account name / avatar / ref, scopes granted,
per-channel daily cap, token expiry, token source (`oauth` | `env` |
`none`), the platform review/audit notice, and the working fallback
while review is pending. Never hardcoded. The Channels page uses this
endpoint; `GET /accounts` remains as the legacy account-record
endpoint for compatibility.

### `GET /auth/{platform}/start`

Returns `{authorize_url, redirect_uri, state}` for the Connect button
to open in the browser. `409` when the platform app credentials are not
configured yet — save them in Settings first.

### `GET /auth/callback/{platform}?code=&state=`

Platform redirect target. Exchanges the code for tokens (single-use,
10-minute state), fetches the account profile, stores the token
encrypted, and upserts the `SocialAccount` row. Returns a small HTML
page ("connected — close this window"); `400` with the exact reason
when the platform refused or the state is bad.

### `POST /channels/{platform}/refresh` · `POST /channels/{platform}/disconnect`

Refresh (YouTube/TikTok refresh-token grant) or extend (Meta 60-day
long-lived exchange) the stored token; disconnect deletes it and marks
the account row `disconnected`. Both return the updated channel card.

### `GET /settings` · `PUT /settings`

Masked credentials + preferences + environment status. `GET` returns
`configured` + `last4` only — never a secret value. `PUT` is a partial
update: omitted fields are kept, `""` on a secret clears it. Covers the
LLM key / base URL / model, per-platform client id/secret, dry-run
override, default privacy per platform, data folder, FFmpeg status, and
monetization thresholds.

### `POST /settings/test-connection/{service}`

Live auth check for `youtube` | `instagram` | `facebook` | `tiktok` |
`llm`, using the stored credential. `200` with `{ok: true, message}`
on success; `502` with the exact provider error otherwise (e.g. token
expired, key rejected 401, network failure) — never a bare 500.

### `GET /ffmpeg/status` · `POST /ffmpeg/install` (v0.4.1)

One-click FFmpeg install. `POST /ffmpeg/install` (202) starts the
install in the background and returns immediately; `GET /ffmpeg/status`
is polled for progress (`state`: idle / downloading / verifying /
extracting / installing / done / error / unsupported, `progress_pct`
0–100, `downloaded_bytes` / `total_bytes`, plain-language `error`).
The server downloads the official build for the platform — Windows:
`ffmpeg-release-essentials.zip` from gyan.dev (the build provider
linked on https://ffmpeg.org/download.html), Linux: the static build
from johnvansickle.com (also linked from ffmpeg.org) — verifies the
published SHA-256 checksum when one is published, extracts to
`<data_dir>/ffmpeg/`, verifies `ffmpeg -version` runs, and saves the
path in settings. On Windows the `bin` folder is also added to the
user PATH (registry `HKCU\Environment`, so new terminals find it too);
inside ClipForge clipping works immediately with no restart, because
the app always prefers the installed binary. A failed install leaves
no half-extracted files behind. `POST /ffmpeg/install?force=true`
re-installs over an existing FFmpeg; without it, an already-installed
FFmpeg answers `done` at once. macOS answers `422` (not auto-installed
yet — `brew install ffmpeg` is the one-command route). The same
installer is available from the command line:
`python scripts/install-ffmpeg.py [--data-dir ./data] [--force]`.

### `GET /yt-dlp/status` · `POST /yt-dlp/install` · `POST /yt-dlp/recheck` (v0.8.1, re-check v0.9.1)

One-click yt-dlp install — the same pattern as FFmpeg, for the tool
that downloads YouTube sources. `POST /yt-dlp/install` (202) starts
the install in the background and returns immediately;
`GET /yt-dlp/status` is polled for progress (`state`: idle /
downloading / verifying / installing / done / error / unsupported,
`progress_pct` 0–100, `downloaded_bytes` / `total_bytes`,
plain-language `error`). The server downloads the official release
binary for the platform — Windows: `yt-dlp.exe`, Linux:
`yt-dlp_linux`, macOS: `yt-dlp_macos`, all from
`https://github.com/yt-dlp/yt-dlp/releases/latest/download/` — checks
its SHA-256 against the release's published `SHA2-256SUMS` when that
file is reachable (a mismatch is a hard failure; a missing sums file
is reported honestly as `checksum_verified: false`), verifies
`yt-dlp --version` runs, and installs it to `<data_dir>/yt-dlp/`.
On Windows the folder is also added to the user PATH (registry
`HKCU\Environment`, so new terminals find it too); inside ClipForge
ingest, live capture, channel fetch and trending match work
immediately with no restart, because the app always prefers the
installed binary. A failed install leaves no half-installed files
behind. `POST /yt-dlp/install?force=true` re-installs over an
existing yt-dlp; without it, an already-installed yt-dlp answers
`done` at once. An unknown platform answers `422`. The same installer
is available from the command line:
`python scripts/install-ytdlp.py [--data-dir ./data] [--force]`.
A job blocked at Ingest with `yt_dlp_missing` is retryable from the
Pipeline page once the install finishes — the install button sits
right on the blocked Ingest stage, next to Retry.

Detection (v0.9.1): `GET /yt-dlp/status` reports `found` only when a
yt-dlp binary both exists and runs (`--version`), with `path`,
`version`, and `source` — where it was found (saved install
location, app data folder, PATH, or a standard install location
such as the pip user Scripts folder or a winget shim). A binary
that exists but does not run is reported as `broken` with a
plain-language `broken_reason`, never as "found". `POST
/yt-dlp/recheck` re-probes on demand with no restart: on Windows it
first re-reads the user + system PATH from the registry into the
running process, so a yt-dlp installed after the server started
(PowerShell / pip / winget) is picked up at once, and it returns
the same status dict plus `path_refreshed` (the PATH dirs the
registry added). The Re-check button sits next to the install
button on the blocked Ingest stage, the Agent page, and Settings.
(The worker gate uses the same detection, so a re-check that finds
yt-dlp unblocks the run.)

### `GET /analytics` · `GET /analytics/{account_id}`

Retention / viewed-vs-swiped signals feeding the next clip picks.

### `POST /scheduler/run`

Run due posts now (dry-run by default). Same engine the scheduler loop
uses: caps, idempotency, backoff.

### `GET /scheduler/status`

Runner state (see `docs/API-CONTRACT.md` §4): `{running, last_run_at,
next_run_at, due_now, last_error, last_run: {id, status, dry_run,
processed, failed_count, duration_s} | null}`. `next_run_at` is
`null`: the scheduler only runs on demand — no cron schedule is
configured in this release. `running` is true only while a run is
executing (runs are synchronous, so it is almost always false between
calls).

## Live workflow + automation (v0.3.0)

The end-to-end pipeline is a visible live workflow — see
`docs/workflow.md` for a curl walk-through. Every job exposes its live
stage, per-stage progress %, an ETA, stage logs, retry/cancel, and an
honest error state. Nothing is faked: a step that could not run because
a key or tool is missing leaves the job `blocked` and names the exact
missing dependency.

### Pipeline stages

| Stage | Progress | What happens |
|---|---|---|
| `ingest` | 0–10% | Upload or YouTube download (own content only). |
| `transcribe` | 10–30% | Whisper transcription (or the fallback-text demo path). |
| `score` | 30–45% | Highlight scoring; candidates below `MIN_SCORE_THRESHOLD`
  (50) become alternates, never clips. |
| `render` | 45–80% | 9:16 reframe + captions per qualifying clip. |
| `seo` | 80–90% | Per-platform title/description/hashtag variants. |
| `review` | 90–100% | Human review. The job ends here unless the
  auto-approve-high-score rule approved a clip (then `ready`). |

### `GET /jobs/{id}` (detail)

Adds to the list shape: `stage`, `progress_pct`, `eta_s` (seconds,
from the average of the last 20 finished worker runs; `null` with no
history), `demo` (true when the transcript came from the fallback-text
path), `blocked_by` (`{code, dependency, kind, detail,
required_action} | null`), `stages` (per-stage `{stage, status,
started_at, finished_at, duration_s, detail}`), `worker_runs` (last 5),
`failed_stage` (latest failed/blocked stage, or `null`),
`started_at`, `finished_at`, `duration_s` (latest worker run),
`alternates` (below-threshold candidates from the latest score stage).

`blocked` vs `failed`: a missing dependency (no FFmpeg, no yt-dlp,
no LLM key) → job `blocked` (retryable once the dependency exists);
a missing/invalid source file → job `failed`.

### `GET /dependencies`

Machine + key inventory, presence only, never values:
`{dependencies: [{name, kind, present, detail, required_action}],
blocked: [...]}`. Kinds: `tool` (ffmpeg, ffprobe, yt-dlp), `package`
(faster-whisper, cv2), `api_key` (`OPENAI_API_KEY`,
`CLIPFORGE_LLM_API_KEY`, `YOUTUBE_REFRESH_TOKEN`,
`META_ACCESS_TOKEN`, `TIKTOK_ACCESS_TOKEN`), `flag`
(`TIKTOK_DIRECT_POST_OPT_IN`).

### `POST /jobs/upload` → `201`

Multipart upload (`.mp4` / `.mov`, up to 10 GiB) with the same
`title` / `own_content` / `transcript_text` / `top_n` / `caption_style`
fields as `POST /jobs`. Creates the job in `pending`; run it with
`POST /jobs/{id}/run`.

### `POST /jobs/{id}/run` · `POST /jobs/{id}/retry` · `POST /jobs/{id}/cancel`

- `run`: run a pending/queued job synchronously (200, job detail).
- `retry`: reset a `failed` or `blocked` job to `pending`, drop its
  candidates + SEO, keep stage/run history, and re-run it (200, same
  job — no new id, so no `retry_of` field). 409 for any other status.
- `cancel`: cancel a pending/queued/failed/blocked job (200); closes
  any orphan running worker run. 409 otherwise.

### `GET /jobs/{id}/logs`

`{logs: [{ts, stage, level, message}]}` — the stage event log,
newest last. A failed/blocked stage's error is here and in
`failed_stage` on the job detail.

### `GET /worker/queue` · `POST /worker/run-next`

The background queue, visible via API: `{running: [{job_id, job_title,
status, stage, started_at, duration_s}], queued: [{id, title,
source_type, status, created_at}], next: {id, title} | null,
recent_runs: [...]}`. `POST /worker/run-next` runs the oldest
queued/pending job synchronously and returns `{ran, queue}` (or
`{ran: null, queue}` when the queue is empty). The worker itself is
`python -m clipforge.worker run <id> | run-next | queue`
(options `--database-url` / `--data-dir` come before the subcommand).

### `GET /automation/feed?kind=&job_id=&clip_candidate_id=&limit=`

Every automation action + skip, newest first: `{id, kind, action,
detail, job_id, clip_candidate_id, post_id, created_at}`. This is the
audit trail — what the automation did, and what it deliberately did
not do.

### `GET /automation/rules` · `PUT /automation/rules`

`GET` → `{rules: [{id, key, kind, label, name, enabled, description,
config, updated_at}]}`. `key` is the frontend toggle key
(`retention_feedback`, `auto_seo`, `staggered_schedule`,
`auto_approve_high_score`); `kind` is the backend rule name
(`auto_pick_next_clip`, `auto_seo_on_approve`, `auto_queue_approved`,
`staggered_schedule`, `auto_approve_high_score`).

`PUT /automation/rules` `{key, enabled}` → the updated rule. 409 only
for `dry_run_guard` (server-locked via `CLIPFORGE_DRY_RUN`); 422 for
an unknown key. `POST /automation/rules` upserts a rule;
`PATCH /automation/rules/{id}` edits one.

Rules (defaults): `auto_seo_on_approve` on, `auto_pick_next_clip` on,
`auto_queue_approved` on, `staggered_schedule` on,
`auto_approve_high_score` **off** (score ≥ 95 + hook ≥ 9 approves the
*clip* only — post approval before publishing still applies).

### `POST /automation/run` · `POST /automation/pick-next/{job_id}`

- `run`: one automation pass — auto-pick the next clip per job
  (retention feedback: early drop-off penalises the hook, retaining
  formats are boosted) and auto-queue approved clips.
  `{run: {picked: [...], queued: [...]}}`.
- `pick-next`: the next clip for one job, with the retention
  adjustment explained (`{candidate_id, score, adjusted_score,
  reason, ...}`).

Auto-queue only queues platforms whose *channel* is connected
(`GET /channels`: OAuth token or legacy env token). A bare
`SocialAccount` row (`GET /accounts`, legacy) does **not** connect a
channel — it only links the post and carries the cap override. Queued
posts are `pending_approval` and respect the per-platform daily caps
(channel card value: provider default or the account override).
Staggering puts each platform's post in its own window (Instagram 11,
Facebook 13, YouTube 15, TikTok 19 UTC).

### `GET /clips/{candidate_id}/seo`

Stored SEO variants for one clip: `{candidate_id, seo: {platform:
{title, description, hashtags, ...}}}`.

## Errors

| Status | Meaning |
|---|---|
| 400 | Malformed request / unknown platform |
| 404 | Unknown job / candidate / post / account |
| 409 | Approval-first violation (unapproved candidate or post); duplicate idempotency key; live publishing disabled while dry-run is on |
| 422 | Missing ownership declaration; missing source; trim outside the candidate span; invalid platform value |
| 502 | Platform adapter error (post marked `failed`, error stored on the post) |

## Dry-run semantics

- `dry_run=true` (default on publish, and dry-run on in Settings or
  `CLIPFORGE_DRY_RUN=true` in the environment): adapters validate
  everything and return the exact payload they *would* send. No token
  is used, no post is created.
- `dry_run=false`: only meaningful with real, approved OAuth tokens,
  approved posts, and dry-run off in Settings. Flip it per environment,
  never per request, and never in a test.
