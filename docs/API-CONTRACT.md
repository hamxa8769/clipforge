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
