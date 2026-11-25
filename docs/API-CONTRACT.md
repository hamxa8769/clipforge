# API contract needed by the v0.3.0 frontend

The v0.3.0 web UI (dashboard, automation view, channels, job pipeline)
works against the existing FastAPI endpoints only. Everything below is
either **already available** (used today) or **needed** — a field or
endpoint the frontend currently works around, with the exact shape it
expects. No frontend change invents a backend field; where a field does
not exist yet, the UI says so instead of guessing.

## Already available (used by the frontend today)

| Endpoint | Used for |
|---|---|
| `GET /health` → `{status, dry_run}` | Liveness + dry-run banner; decides live vs labelled demo data. |
| `POST /resolve` → `{kind: "video", video} | {kind: "channel" \| "playlist", channel, videos[], source}` (v0.11.0) | Dashboard one-box: a pasted video/channel link resolves to the video or the channel's newest videos (newest first, capped at `max_results`, default 25) with no job created. Video links are probed metadata-only; channel links list via yt-dlp flat-playlist extraction (no key needed) or the saved YouTube Data API key when present (`source: "api"` vs `"ytdlp"`). Unresolvable links are 422 with a plain-language reason. |
| `POST /jobs/clear-finished` → `{deleted, ids, statuses}` (v0.11.0) | Deletes failed / cancelled / blocked jobs (with their candidates, posts, stage events, worker runs and clips folder) so the Jobs list stays clean. Review / ready / running jobs are never touched. |
| `GET /jobs` / `GET /jobs/{id}` | Jobs, statuses, `error`, `source_type`, `own_content`, `candidates` count. |
| `GET /jobs/{id}/candidates` | Review list, SEO variants, trim spans. |
| `POST /jobs/{id}/candidates/{n}/approve` / `reject` / `trim` | Review actions. |
| `GET /posts` (+ `?status=` `?platform=`) | Approval queue, schedule, automation "what is next". Returns `error`, `attempts`, `scheduled_for`, `publish_result`. |
| `POST /posts/{id}/approve`, `POST /posts/{id}/publish?dry_run=` | Queue actions. |
| `POST /posts` (from an approved clip) | Creating a scheduled post. |
| `GET /accounts` | Channels strip, Channels page, Settings. Returns `provider`, `label`, `account_ref`, `status`, `token_env_var`, `token_configured`, `daily_cap_override`. |
| `GET /analytics` | Per-platform stats cards, retention signals (`signals[]`), monetization inputs. Returns newest-first snapshots: `platform`, `account_id`, `date`, `views`, `followers`, `watch_hours`, `shorts_views_90d`, `completion_pct`, `viewed_pct`, `signals`. |
| `GET /monetization?platform=&subscribers=&followers=&watch_hours=&shorts_views=&views_30d=` | Threshold progress. **The frontend always passes real metrics** (from the latest analytics snapshot); the bare endpoint defaults every metric to 0 and would show every live channel at 0%. |
| `POST /scheduler/run?dry_run=` | "Run scheduler now" on the Automation page. Returns `{dry_run, results: [{post_id, status, message}]}` where `status` is `published` \| `dry_run` \| `draft_created` \| `skipped_cap` \| `failed`. |

## Needed — frontend workarounds in place today

### 1. Automation rules endpoint (needed for real toggles) — IMPLEMENTED (v0.3.0)
The Automation page's rule toggles (retention-feedback clip picking,
auto-SEO, staggered scheduling, auto-approve ≥95) are currently stored
in the browser's `localStorage` (`clipforge.automation.rules.v1`) and
labelled "stored in this browser only". The clip picker and scheduler
do not read them.

**Implemented:** a server-side rules store:
- `GET /automation/rules` → `{rules: [{key, label, enabled, description}]}`
- `PUT /automation/rules` `{key, enabled}` → the updated rule (or 409
  when the rule is server-locked, e.g. the dry-run guard).

The frontend may now drop the localStorage workaround and bind the
toggles to these endpoints. Rules default-seed on first read, so the
endpoints work even if no rule was ever created. `key` accepts the
frontend toggle key (`retention_feedback`, `auto_seo`,
`staggered_schedule`, `auto_approve_high_score`) or the backend kind
(`auto_pick_next_clip`, `auto_seo_on_approve`, `auto_queue_approved`).
Unknown keys → 422. The full rule object also carries `id`, `kind`,
`name`, `config`, `updated_at`. See `docs/api.md` "Live workflow +
automation" and `docs/workflow.md`.

### 2. Job run detail: failed stage + per-stage logs + duration — IMPLEMENTED (v0.3.0)
The job detail page derives the 8-stage pipeline from the `JobStatus`
enum only. For a **failed** job the UI deliberately marks no stage as
failed — the backend reports `error` (string) but no failed stage.

**Implemented (all additive, on `GET /jobs/{id}` detail):**
- `failed_stage: "ingest" | "transcribe" | "score" | "render" | "review" | null`
- `started_at: string | null`, `finished_at: string | null`
  (lets the Automation history show real durations instead of only the
  created date), and `duration_s: number | null`.
- `GET /jobs/{id}/logs` → `{logs: [{ts, stage, level, message}]}`.
- v0.9.0 queue + plain-language errors: `queue_position: number | null`
  (1-based position among waiting jobs; `null` when not queued),
  `queue_priority: number` (1 = user-started, 0 = batch/agent),
  `error_title: string | null`, `error_action: string | null`,
  `error_code: string | null` — the failed job shows `error_title`
  plus the plain-language `error` and a Retry button; no raw
  traceback is ever returned. `POST /jobs/{id}/run?wait=` and
  `POST /jobs/{id}/retry?wait=` enqueue at priority 1 and, with
  `wait=true` (default), block until the job finishes; `wait=false`
  returns at once with the job's `queue_position`.

The job page can now mark the failed stage and show the per-stage log
instead of only the single `error` string. The detail also carries
`stage`, `progress_pct`, `eta_s`, `demo`, `blocked_by`, `stages[]`,
`worker_runs[]` and `alternates[]` (below-threshold candidates).

### 3. Job retry endpoint (nice-to-have; worked around) — IMPLEMENTED (v0.3.0)
Retry currently re-creates the job via `POST /jobs` with the same
`source_type` + `source_path`/`source_url` and a fresh own-content
declaration. That works, but it creates a **new job id** and loses the
link to the failed attempt.

**Implemented:** `POST /jobs/{id}/retry` → `200` (same job re-queued).
It resets a `failed` or `blocked` job to `pending`, drops its
candidates + SEO, keeps the stage/run history, and re-runs it. No new
job id is created, so there is no `retry_of` field (the contract
allows the `200` same-job form). 409 for any other status.
`POST /jobs/{id}/cancel` (pending/queued/failed/blocked → `cancelled`)
and `POST /jobs/{id}/run` also exist.

### 4. Scheduler status endpoint (nice-to-have) — IMPLEMENTED (v0.3.0)
The Automation page polls `GET /jobs` + `GET /posts` to infer runner
state. A dedicated endpoint would be cheaper and more truthful:

**Implemented:** `GET /scheduler/status` →
`{running: boolean, last_run_at: string | null, next_run_at: string | null,
due_now: number, last_error: string | null, last_run: {id, status,
dry_run, processed, failed_count, duration_s} | null}`.
`next_run_at` is `null`: the scheduler runs on demand only
(`POST /scheduler/run`), no cron schedule is configured in this
release. Every `POST /scheduler/run` records a `SchedulerRun` row,
so `last_run` / `last_error` are truthful.

### 5. Per-platform analytics ingest (already designed, not yet live)
`GET /analytics` returns `[]` until an ingest runs, and ingest returns
`not_configured` without `YOUTUBE_REFRESH_TOKEN` / `META_ACCESS_TOKEN` /
`TIKTOK_ACCESS_TOKEN`. The frontend handles the empty case with an
honest "not connected yet" state. No frontend change is needed here —
this is a server/credentials task (plus each platform's app review /
audit for live posting).

## 6. Notifications feed + job pause/resume + post reject (v0.9.0, Worker C)

All three are IMPLEMENTED on the server and wired in the dashboard
(Worker D). This section is the contract the UI relies on.

### 6a. Notifications centre — IMPLEMENTED
- The header bell uses the UI ALIASES (app.py, "notifications: UI
  aliases" — explicitly "the shape the header bell is built against"):
  `GET /notifications` -> `NotificationItem[]`, newest first. One
  item: `{id, title, body, tone, read, created_at, link}` where `tone`
  is `danger | warning | success | info` (stored level `error` ->
  `danger`) and `link` is an in-app route (e.g. `/queue`,
  `/jobs/<id>`) or null.
- `POST /notifications/read {ids?: string[]}` -> `{read: number}`.
  Omit `ids` to mark all read. `POST /notifications/clear` ->
  `{cleared: number}`.
- The canonical API is the same store in a different shape:
  `GET /api/notifications?unread_only=&limit=` ->
  `{notifications, unread}` (note: the key is `unread`, NOT
  `unread_count`) with items in `{id, level, title, message, action,
  platform, job_id, read, created_at}` shape; `POST
  /api/notifications/{id}/read`, `/read-all` (`{marked_read, unread}`)
  and `/clear` (`{cleared, unread}`). `POST /api/notifications
  {level, title, message, action?, job_id?, platform?, dedupe_key?}`
  creates one (201); `action` is a path (`/channels`) or a
  `reconnect:<platform>` hint. One store serves both shapes:
  `src/clipforge/auth/notifications.py` (the `notifications` table +
  `emit_notification`, 24 h dedupe) is canonical; the thin adapter
  `src/clipforge/notifications.py` (Worker C) reuses it. The earlier
  standalone router file `apps/api/clipforge_api/notifications.py`
  was deleted as a duplicate — do not recreate it.
- Events worth notifying about: a job finished / failed, a clip is
  waiting for approval, a channel token is about to expire / expired,
  an update is available. The header bell polls every 60s.

### 6b. Job pause / resume / cancel — IMPLEMENTED (job queue engine)
- `POST /jobs/{id}/pause` — queued or running jobs. A running job
  pauses between stages / clips (a clip already rendering finishes
  first), then waits in `paused` until resumed. 409 when the job is
  not queued / running.
- `POST /jobs/{id}/resume` — paused jobs only. Continues from the
  next stage — nothing is re-done; a job paused while queued goes
  back to `queued`. 409 when the job is not paused.
- `POST /jobs/{id}/cancel` — anything not terminal (pending, queued,
  running, transcribing, scoring, rendering, paused, failed,
  blocked). A running job reports `cancelling: true` until the worker
  confirms it and the job lands in `cancelled`. A job in review /
  ready cannot be cancelled from here (409).
- New `JobStatus` values on the wire: `queued`, `running`, `paused`,
  `cancelled`. `GET /worker/queue` also returns `paused[]` and
  `active_job_id`, and queued entries carry an honest 1-based
  `queue_position`.

### 6c. Post reject — NOT IMPLEMENTED
- No `POST /posts/{id}/reject` endpoint exists (only candidate reject:
  `POST /jobs/{id}/candidates/{n}/reject`). The dashboard's inline
  Reject button is disabled with that reason. `POST
  /posts/{id}/approve` and `POST /posts/{id}/publish` are real and wired
  to the dashboard.

## Conventions the frontend relies on

- Status enums are exactly the backend's (`JobStatus`, `PostStatus`);
  unknown values are normalised to `pending` / `pending_approval`
  (see `apps/web/lib/api.ts`) — the UI never renders a raw unknown status.
- `GET /monetization` returns one row per program (YouTube returns
  three: YPP Early, YPP Full, YPP Full — NEW APPLICANTS FROM 1 FEB 2027;
  Instagram/Facebook two each). Threshold keys are the backend's
  (`shorts_views_90d_alt`, `followers`, `watch_hours_12mo`, …).
- Tokens are never sent to, or shown by, the UI: `token_env_var` is a
  **name** only, and `token_configured` is a boolean.
