# ClipForge v0.9.0 — Audit (Worker A)

**Branch:** `feature/v0.9.0-production-saas` · **Date:** 2026-11-23 (PKT)
**Auditor:** Worker A (QA / debugger). **Scope:** every page + every control,
end-to-end against the real API (fresh data dir), plus the fixes in
`FIXES-v0.9.0-worker-a` below.

> **Tree-state warning — read first.** Other workers were editing the same
> uncommitted tree during this audit (23 Nov ~22:30–04:00 PKT). Two blocking
> defects found early in the audit were **partly fixed in-flight by other
> workers before this doc was written** (A1 fully, A2 only on the create
> path — see §1). Every finding below is tagged with the tree state it was
> verified on:
> - **OLD** — original server, code as of ~22:38 PKT, 22 Nov.
> - **SCRATCH** — rsync snapshot of the tree (~22:49 PKT, 23 Nov) with one
>   local-only shim (see §6); used to test the new queue engine.
> - **REAL** — the live repo tree, re-verified 23 Nov ~23:00 PKT on port 8125
>   with a fresh data dir (`~/workspace/audit-cf/data-real`).
>
> A finding marked OLD/SCRATCH that is **not** re-confirmed on REAL may
> already be fixed. A finding marked REAL is current as of this writing.

**Test environment:** Linux VM (not Windows — no Tauri/NSIS/Job-Object
verification possible), API on 127.0.0.1:8123/8125, fresh SQLite data dirs,
real 60 s local sample video (`audit-cf/sample.mp4`, FFmpeg testsrc2),
supplied transcript text. No real channels, no API keys, no Whisper
backend — see §6 Limits.

## Verdict counts (controls audited)

| Verdict | Count | Meaning |
|---|---|---|
| WORKS | ~95 | Control works end-to-end against the real API |
| BROKEN | 6 | Control fails when used as labelled |
| DUMMY | 4 | Control looks interactive but does nothing / is local-only |
| SILENT-FAIL | 3 | Control fails without a usable error, or lies about the result |
| **Total audited** | **~108** | Pages, buttons, forms, toggles, links, wizard steps |

Counts are for *controls*, not pages — most pages are mostly working; the
defects below are concentrated in a handful of workflows.

## 1. Top 10 defects by user impact

| # | Defect | Severity | Status |
|---|---|---|---|
| 1 | **No way to start a job from the UI, and nothing auto-runs it.** A job created from Dashboard/Pipeline sits `pending` forever; no Run button exists anywhere; the Dashboard says "Clip job started — cutting clips now" after only creating it. The queue engine (Worker B) works server-side, but the UI never calls it. | Critical | **Open — Worker A fix in this round** (Run/Pause/Resume/Cancel buttons added to Pipeline; see §4) |
| 2 | **Batch Mode is completely broken from the UI.** `startBatch` sends `{sources: [{url\|path}]}` (objects) but the server requires `list[str]` → 422 on every batch start. Also no rights checkbox, so YouTube URLs 422 even with the right shape. | Critical | **Fixed by Worker A** (see §4) |
| 3 | **Approval Queue lies.** Approve/Publish set status optimistically without checking the response (`catch { /* demo */ }`). In demo/offline mode it marks posts approved that were never approved. | Critical | **Fixed by Worker A** |
| 4 | **Approving a clip with no channel connected creates zero posts, UI says "sent to the approval queue".** Verified live: `automation.queue` all `skipped_no_account`, `GET /posts` → `[]`. | High | **Fixed by Worker A** (honest message + link to Channels) |
| 5 | **Trending links 404.** `Trending` links to `/jobs/<live-id>`; the static export has no such page (only demo `job-001`–`job-004` + `jobs/view`). The `jobs/[id]` page header itself warns about this. | High | **Fixed by Worker A** (→ `/jobs/view/?id=…`) |
| 6 | **Export Hub can never complete.** One free-text "Clip ID" is used for both the copyright scan (needs a *clip candidate* id) and publish (needs a *post* id) — a correct id for one 404s on the other. Also contradicts itself on auto-trim (says the clip "will be cut to 60 s" while disabling publish for too-long clips). | High | **Fixed by Worker A** (post picker: scan uses `post.clip_id`, publish uses `post.id`) |
| 7 | **Studio is a dead end.** Pro Mode settings (caption style, punch-in, aspect), trim range and ripple cuts are local-only state; there is no Render / Create-clip-job action and nothing persists. Transcript hint also lies ("without transcript lines the whole video is analysed as one segment" — the server 422s text-less segments; transcript is required). | High | **Partially fixed by Worker A** (honest labelling + corrected hint; render path still missing — needs a product decision, see §5) |
| 8 | **Connect wizard step 5 shows "Connected" without checking status.** Reachable by deep link (`?step=5`) while not connected. | Medium | **Fixed by Worker A** |
| 9 | **Batch item errors are invisible.** A failed batch item shows only "failed" — the server's error text (`it.error`) is never rendered. | Medium | **Fixed by Worker A** |
| 10 | **"AI-assisted edit" checkbox on the review page is local-only.** `aiLabel` state is never sent anywhere; no endpoint accepts it. | Medium | **Fixed by Worker A** (replaced with an honest static note) |

### Fixed in-flight by other workers (found by this audit, no longer open)

| Defect | Evidence | Status |
|---|---|---|
| **A1 — Server would not start:** `ModuleNotFoundError: No module named 'clipforge.auth.notifications'` (file deleted from tree; imported by `apps/api/clipforge_api/app.py:129-136`, `src/clipforge/auth/health.py:45`, `src/clipforge/notifications.py:37-48`). Earlier failure mode was a duplicate `Notification` model in `src/clipforge/db/__init__.py` (two classes, same `__tablename__ = "notifications"`). | OLD/SCRATCH | **FIXED — re-verified on REAL:** `src/clipforge/auth/notifications.py` restored; real tree starts, `/health` → `{"status":"ok",...}` |
| **A2 — POST /jobs returned 500 but created the job:** `TypeError: can't compare offset-naive and offset-aware datetimes` in `app.py:511` `_queue_position` → `worker.queue_order_key` (aware `datetime.min.replace(tzinfo=UTC)` fallback vs naive DB `created_at`; just-created job has `created_at=None` in-session). Every UI retry created a duplicate job. | OLD/SCRATCH | **PARTIALLY FIXED — create path verified, root cause still live (see correction below).** POST /jobs → 200 on REAL (job `c80ae3dd…` created once). BUT `src/clipforge/worker.py:121` still returns `job.created_at or datetime.min.replace(tzinfo=UTC)` — an aware fallback mixed with naive DB datetimes — so `sorted(..., key=queue_order_key)` in `app.py:503-517` `_queue_position` still raises the same `TypeError` whenever a pending/queued job has `created_at=None`. Proven by Worker B's own new tests failing on the current tree: `tests/unit/test_job_queue.py` (5–6 failures, run-dependent) and `tests/integration/test_workflow_api.py::test_same_video_different_transcript_text_not_cached`. The live 200 only holds while every queued job has a non-null naive `created_at`. **Routed to Worker B** (R1) — the fix is a one-line normalisation in `queue_order_key` (make the fallback naive, or coerce `created_at` to aware), not a UI fix. |

## 2. Full audit table

### Dashboard (`apps/web/app/page.tsx`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| New Clip Run form (title, source, rights) | WORKS* | Creates the job correctly **now that A2 is fixed** — but see Top-10 #1: it never starts the job, and its success note ("Clip job started — cutting clips now") is false. The form lives in `apps/web/components/NewClipRun.tsx` (Worker C/D file — not edited here). | Critical* |
| Job list / status cards / stats | WORKS | Live data from `GET /jobs`; demo fallback labelled with `DemoBadge`. | — |
| Refresh / navigation | WORKS | | — |
| "Update available" updater button | WORKS* | Shows honest "updater not available in this build" in the browser; Tauri path unverified on this VM (no Windows). | — |

### Pipeline (`apps/web/app/pipeline/page.tsx`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| Job list, filters, stage progress | WORKS | Live from `GET /jobs`; per-stage progress, error text shown for blocked/failed jobs. | — |
| Run / Pause / Resume / Cancel | **BROKEN (was missing)** | No controls existed at all — the queue engine's endpoints (`POST /jobs/{id}/run?wait=false|pause|resume|cancel`) were unreachable from the UI. | Critical |
| Retry (failed/blocked jobs) | WORKS | `POST /jobs/{id}/retry` wired; server validates state (409 otherwise). | — |

### Agent (`apps/web/app/agent/page.tsx`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| Enable/disable toggle | WORKS | Persists via `PUT /api/agent/config`; OFF is persisted (v0.8.1 fix held). | — |
| Start agent run | WORKS | Fail-fast 422s in plain language, verified live on REAL: agent off → "The agent is off — turn it on…"; yt-dlp missing → "yt-dlp is not installed — … Press 'Install yt-dlp'… Or use a local file instead." | — |
| Config form (source, niche, rights, platforms) | WORKS | Persists; rights checkbox required before start. | — |
| Agent status / last error / last run | WORKS | Shows real `last_error` from the server. | — |

### Studio (`apps/web/app/studio/page.tsx`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| Video upload / preview / player | WORKS | Plays local uploads via `/media/uploads`. | — |
| Transcript textarea + Analyse | **BROKEN (copy)** | Label says "optional" and hint says "Without transcript lines the whole video is analysed as one segment" — false. `POST /detection/analyze` with a text-less segment → 422 "Too little transcript to cut a clip — a clip needs at least 15 seconds of transcript." The UI's own guard errors first, so the hint can never be true. | High |
| Pro Mode settings (caption style, punch-in zoom, aspect ratio), trim range, ripple cuts | **DUMMY** | Local-only `useState`; nothing persists, no render/create action exists, no server endpoint accepts these settings. Page copy claims "These settings are applied when the clip is rendered" — there is no render path from Studio. | High |
| Clip candidates from Analyse | WORKS | Real scoring from `/detection/analyze` (verified: 1 candidate, score 18.5, real reason text). | — |

### Export Hub (`apps/web/app/export/page.tsx`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| Clip ID text field + copyright scan + Publish + Publish to X | **BROKEN** | One id is used for both `/publish/scan` (needs a **clip candidate** id — `_get_clip_or_404` → `ClipCandidate`) and `/posts/{id}/publish` (needs a **post** id). Verified: publishing real candidate id `69e00de6c13d439c` → 404 `post … not found`. The flow can never complete with one id. | High |
| "Too long" badge | **BROKEN (copy)** | Says "Auto-trim: the clip will be cut to the first 60s in Clip Studio before publishing" while the Publish button is disabled for too-long clips — contradictory. | Medium |
| Affiliate link / A-B variants forms | WORKS* | Correctly use candidate ids; server 404s unknown ids with a readable message. Placeholder `clip_01` is not a real id — user must paste a real candidate id (no picker). | Low |

### Batch (`apps/web/app/batch/page.tsx`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| Start batch | **BROKEN** | UI sends `{sources: [{url\|path}]}` objects; server `BatchIn.sources: list[str]` (`apps/api/clipforge_api/batch.py:37-39`) → 422 `Input should be a valid string` at `body.sources.0`. Verified on OLD, SCRATCH and REAL. Every UI batch start fails. | Critical |
| Rights / own-content declaration | **BROKEN (missing)** | No checkbox in the UI, yet the server 422s YouTube URLs without `own_content_declaration=true`. | Critical |
| Batch item list | **SILENT-FAIL** | Shows state/progress/clip count but never `it.error` — a failed item (e.g. faster-whisper missing) shows only "failed". | Medium |
| "Drop / choose a file list" | **DUMMY (copy)** | File chooser only — no drag-drop handler exists. | Low |
| Start batch after a batch finishes | **BROKEN** | Button stays disabled while `batchId !== null` until "Clear all" (which also clears sources). | Low |
| Batch engine (server-side) | WORKS | Correct shape → 202, real processor runs jobs through the queue; pause API works; item errors are clear server-side. | — |

### Trending (`apps/web/app/trending/page.tsx`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| "Use as source" / open-job links (3 places) | **BROKEN** | Link to `/jobs/${id}`; static export has no `out/jobs/<live-id>/index.html` (only demo `job-001`–`job-004` + `jobs/view`). `jobs/[id]/page.tsx` header itself warns direct `/jobs/<live-id>` 404s from the static file server. | High |
| Trending list, scores, filters, run scan | WORKS | Live from `/api/trending/*`; run scan works. | — |

### Growth (`apps/web/app/growth/page.tsx`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| Charts, snapshots, platform breakdown | WORKS | Live from `/growth*` / `/analytics`; empty state is honest (`[]`, "not measured yet" — no invented views). | — |
| Refresh / snapshot actions | WORKS | | — |

### Channels + Connect wizard (`apps/web/app/channels/*`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| Channel list / status cards | WORKS | Live from `GET /channels`; all `not_connected` with review notices when unconfigured. Demo cards (`@clipforge-demo` "connected" YouTube) are labelled demo, but the green "Connected" badge on a fake account is still misleading. | Low |
| Connect wizard steps 1–4 | WORKS* | Guide text, env-var-name fields (never accepts token values — server rejects `sk-`/`EAAB`/long values with 422). OAuth flow itself **not verifiable here** (no real accounts / no callback). | — |
| Connect wizard step 5 badge | **BROKEN** | Shows green "Connected" whenever `channel` is set, without checking `channel.status`; reachable via `?step=5` deep link while not connected. | Medium |
| Refresh / reconnect / disconnect buttons | WORKS* | Endpoints exist and validate; token refresh **not live-verified** (no credentials) — routed to Worker B/C, see §5. | — |

### Approval Queue (`apps/web/app/queue/*`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| Approve / Publish buttons | **SILENT-FAIL** | `QueueClient.tsx:18-25` fires raw `fetch(.../posts/{id}/{action})` and **always** sets status to `approved`/`dry_run` without checking the response (`catch { /* demo */ }`, no `res.ok` check). In demo/offline mode it marks demo posts approved. | Critical |
| Queue list, filters, post detail | WORKS | Live from `GET /posts`. | — |

### Job review (`apps/web/app/jobs/[id]/ReviewClient.tsx`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| Approve candidate | **SILENT-FAIL** | Sets note "Approved — sent to the approval queue." on any `res.ok`, ignoring `res.data.automation.queue`. Verified live: with no channel connected, approval created **zero** posts (`automation.queue` all `skipped_no_account`, `GET /posts` → `[]`). | High |
| Reject / Trim / SEO / thumbnails / player | WORKS | Real endpoints; trim validates; SEO variants stored per platform. | — |
| "AI-assisted edit" checkbox | **DUMMY** | `aiLabel` local state is never sent to any API; no endpoint accepts it. | Medium |

### Schedule (`apps/web/app/schedule/page.tsx`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| Schedule list, due-now, "Run scheduler now" | WORKS | `POST /scheduler/run?dry_run=true` → `{"dry_run":true,"results":[]}` verified; scheduler status endpoint live. | — |
| Create/edit schedule | WORKS* | Persists; publish goes through the scheduler (dry-run default — honest). | — |

### Monetization (`apps/web/app/monetization/page.tsx`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| Revenue tracker, thresholds, A/B results | WORKS | Honest: `earnings` null unless a platform API reported them; `ctr` always null (no impressions read); A/B views 0 / "not measured yet" until a real outcome exists. | — |
| Affiliate / A-B forms | WORKS* | See Export Hub note — candidate-id based, no picker. | Low |

### Settings (`apps/web/app/settings/page.tsx` + sub-pages)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| Dry-run toggle | WORKS | `PUT /settings {dry_run:false}` sticks (verified: `/health` reflects it). Note: when the value comes from env, the UI shows its source (`dry_run_source`) — honest. | — |
| Settings save (keys, preferences) | WORKS | Persists; secrets masked; test-connection endpoints exist per service. | — |
| FFmpeg install (one-click) | WORKS* | Status endpoint live (`ffmpeg 8.1.2 found` on this VM). Real Windows install **not verifiable here**; download path is the official build with progress polling. | — |
| yt-dlp install (one-click) | WORKS* | Status endpoint live (`yt-dlp missing` here). Install flow itself **not verifiable on this VM** (egress proxy TLS MITM blocks the GitHub release fetch — same blocker as v0.8.1). | — |
| Performance settings | WORKS | Persist via settings; `/perf/profile` endpoint live. | — |
| Updates / check for updates | WORKS* | In-browser shows honest "not available in this build"; Tauri updater path unverified (no Windows). | — |
| Gemini / LLM provider setup | WORKS* | `GET /api/llm/providers` → rules engine active, Gemini/OpenAI unconfigured; key test **not verifiable** (no key). | — |

### First-run setup wizard (`apps/web/app/setup/*`, `/api/setup/state`, `PUT /onboarding`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| Steps, progress, resume | WORKS | `GET /onboarding` + `PUT /onboarding` live; progress is resumable; no raw JSON shown. | — |

### Health / startup

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| `/health`, `/dependencies` | WORKS | Live; dependencies honestly report ffmpeg found / yt-dlp missing / faster-whisper missing. | — |
| Startup (desktop) | WORKS* | Server starts cleanly on the current tree (A1 fixed). Windows startup/Job-Object/NSIS behaviour **not verifiable here**. | — |
| Logs drawer / StatusBar | WORKS | Live logs; no raw JSON in main views. | — |

### Automation (`apps/web/app/automation/page.tsx`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| Rules list / create / edit / toggle | WORKS | `GET/PUT/POST/PATCH /automation/rules` live; toggles persist. | — |
| "Run automation now" | WORKS | `POST /automation/run` live. | — |
| Copy claim "New jobs start automatically when created" | **BROKEN (copy)** | False — verified on REAL: a created job stays `pending` after 20 s idle; nothing auto-runs it and the UI never calls `run`/`run-next`. The queue is manual until a dispatcher/auto-run exists (Worker B area — see §5). | High |
| Notifications bell | WORKS* | `NotificationsBell.tsx` (Worker D) shows honest "not available yet" when the endpoint is missing; endpoints now exist server-side; end-to-end (bell → real rows) **not verified against the real tree** (was blocked by A1 during this audit). | — |

### X (Twitter) (`apps/web/app/x/*`, `/x/*`)

| Control | Verdict | Defect / note | Severity |
|---|---|---|---|
| X status / publish | WORKS* | `GET /x/status` honest: "X is not connected… Posting to X needs an X API Basic plan (paid) or higher — the free tier is read-only". Live publish **not verifiable** (no keys, paid tier needed). | — |

## 3. Verified working end-to-end (do not re-test from scratch)

- Full pipeline on a real local 60 s video + supplied transcript (OLD): job
  `588654523fb649a7` → `review`, 2 candidates, rendered `clip_01.mp4`
  (31.8 s, 10 MB) + `clip_02.mp4`, thumbnails + per-platform SEO variants.
  `demo:true` is **correct** there (fallback-text transcript).
- Queue engine server-side (SCRATCH + REAL): `POST /jobs/{id}/run?wait=false`
  enqueues with `queue_position`; only one active job; pause mid-render →
  `paused`; resume → `running` → `review` with 2 candidates; cancel
  mid-run → `cancelled` ("Job cancelled by the user."); cancel of a queued
  job → `cancelled`; pause of a blocked job → 409 (correct). `POST
  /worker/run-next` → 200, runs the next job to `review` (took ~95 s —
  synchronous, so the UI must use `wait=false` + polling, never block on it).
- All UI-called API paths exist on the server (checked `lib/api.ts` paths
  against `/openapi.json`, ~100 routes).
- `apiPostJson` has a 5 s `AbortSignal.timeout` and returns generic
  `Request failed (status)` when FastAPI validation `detail` is a list —
  so 422s surface as gibberish/generic text in the UI (contributes to
  Batch looking "silent"). **Routed:** lib-level error extraction belongs
  to whoever owns `lib/api.ts` — see §5. (Worker A fixed the Batch page
  to show the server's own error text instead.)
- Agent fail-fast, dry-run toggle, scheduler run, X status, setup wizard,
  health/dependencies — all verified live (see table).

## 4. Fixes made by Worker A (this round)

All in files owned by Worker A (audit + small dead-button fixes). No files
owned by Workers B/C/D were edited.

1. **`apps/web/app/batch/page.tsx`** — Batch now works from the UI:
   - sends plain string sources (`sources.map(s => s.url ?? s.path)`) +
     `own_content_declaration` from a new rights checkbox
     ("This is my own content or a Creative Commons / public-domain video —
     CC-BY attribution is added automatically"); the checkbox is required
     before Start.
   - failed items now render `it.error` (previously invisible).
   - "Drop / choose a file list" relabelled "Choose a file list"
     (no drag-drop exists).
   - Start is re-enabled after a batch reaches a terminal state without
     clearing sources.
   - Note: `lib/api.ts` `startBatch` still sends the object shape — the
     page now builds the payload itself (only caller); the lib-level fix
     is routed in §5.
2. **`apps/web/app/pipeline/page.tsx`** — Run / Pause / Resume / Cancel
   buttons per job, wired to `POST /jobs/{id}/run?wait=false|pause|resume|
   cancel` (via `apiPostJson`, with polling refresh); enabled only for the
   statuses each action accepts; server errors are shown, not swallowed.
   This closes Top-10 #1's UI half: jobs can now be started and controlled.
3. **`apps/web/app/queue/QueueClient.tsx`** — Approve/Publish now check
   `res.ok` before updating status; server errors are shown; buttons are
   disabled with a reason when the API is unreachable or the row is a demo
   post. No more optimistic "approved" lies.
4. **`apps/web/app/jobs/[id]/ReviewClient.tsx`** — Approve now reads
   `res.data.automation.queue`: if zero posts were created it says so
   plainly ("Approved, but no posts were created — no channel is connected
   yet") with a link to `/channels`; if posts were created it names the
   platforms. The local-only "AI-assisted edit" checkbox is removed and
   replaced with an honest static note (no endpoint accepts the flag).
5. **`apps/web/app/trending/page.tsx`** — all three `/jobs/${id}` links →
   `/jobs/view/?id=${id}` (the only route the static export serves for live
   jobs).
6. **`apps/web/app/export/page.tsx`** — Export Hub now uses a post picker:
   loads `GET /posts`, the user picks a post from the approval queue; the
   copyright scan uses `post.clip_id`, Publish uses `post.id`, Publish-to-X
   uses `post.clip_id`. Empty queue → honest "no posts yet — approve a clip
   first" state instead of a dead text field. Auto-trim copy fixed (no
   longer claims the clip "will be cut" while disabling publish).
7. **`apps/web/app/studio/page.tsx`** — transcript label/hint corrected
   (transcript lines are **required** — the server 422s text-less segments);
   Pro Mode section now carries an honest note that its settings are a
   preview only and are not saved or sent to the renderer yet (no render
   path exists from Studio — see §5).
8. **`apps/web/app/channels/connect/page.tsx`** — step-5 "Connected" badge
   only renders when `channel.status === "connected"`.
9. **New helpers + tests:** `apps/web/lib/batch.ts` (`buildBatchPayload`,
   `batchItemError`) and `apps/web/lib/review.ts`
   (`summarizeApproveQueue`) with `apps/web/lib/batch.test.ts` /
   `review.test.ts`, wired into `apps/web/scripts/test-unit.sh`.

## 5. Routed defects (other workers — NOT fixed here)

| # | Defect | File / line | Expected behaviour | Owner |
|---|---|---|---|---|
| R1 | ~~POST /jobs 500 datetime bug~~ — create path now 200, but the root cause is still live: `queue_order_key` mixes an aware fallback with naive DB datetimes, so any queue sort over a job with `created_at=None` raises `TypeError` | `src/clipforge/worker.py:111-122` (`job.created_at or datetime.min.replace(tzinfo=UTC)`) / `apps/api/clipforge_api/app.py:503-517` `_queue_position` | Normalise in `queue_order_key` (naive fallback `datetime.min`, or coerce `created_at` to aware) so `sorted()` never mixes tz-aware/naive; Worker B's own `tests/unit/test_job_queue.py` + `test_workflow_api.py::test_same_video_different_transcript_text_not_cached` must go green | Worker B — **still open** |
| R2 | ~~Server won't start (missing notifications module / duplicate Notification model)~~ | `src/clipforge/auth/notifications.py` (was deleted); `src/clipforge/db/__init__.py` (~294 vs ~525) | Server starts; one Notification model | Worker B/C — **already fixed in-flight, verified on REAL** |
| R3 | No auto-run: created jobs stay `pending` forever; Automation page claims they start automatically | `src/clipforge/worker.py` / dispatcher (no auto-run loop exists); `apps/web/app/automation/page.tsx` copy | Either a dispatcher auto-runs new jobs (with a visible "auto-run" setting) or the copy is corrected to "jobs wait for you to press Run" | Worker B (engine) + Worker C/D (copy) |
| R4 | Channels disconnect repeatedly / token refresh reliability ("channels that stay connected" was a v0.9.0 goal) | `src/clipforge/auth/` (channels, token_store, oauth) | Refresh extends tokens silently; disconnect only on a dead grant; health endpoint reports real status | Worker B/C |
| R5 | `lib/api.ts` `startBatch` sends the wrong payload shape (objects, not strings) and never sends `own_content_declaration` | `apps/web/lib/api.ts:2170-2175` | `startBatch` sends `string[]` + `own_content_declaration` (page-level workaround in §4.1 covers the only caller for now) | Owner of `lib/api.ts` |
| R6 | `apiPostJson` turns FastAPI validation-error lists into generic `Request failed (422)` | `apps/web/lib/api.ts:748-758` | Join the first validation message (e.g. `sources.0: Input should be a valid string`) so the UI shows a usable error | Owner of `lib/api.ts` |
| R7 | Studio has no render / create-clip-job path; Pro Mode settings have no server counterpart | `apps/web/app/studio/page.tsx` + (new endpoint needed) | Either a "Create clip job from this range" action (needs a product decision) or the section stays honestly labelled preview-only | Coordinator / Worker B |
| R8 | Notifications end-to-end not verified (bell → real rows); two notification stacks existed during the audit | `src/clipforge/notifications.py`, `apps/api/clipforge_api/notifications.py`, `src/clipforge/auth/notifications.py` | One notification store; bell shows real rows; disconnect alerts fire | Worker B/C/D |
| R9 | Demo channel card shows a fake "Connected" YouTube account (`@clipforge-demo`) | `apps/web/lib/mock.ts` demo channels | Demo badge must be unmissable, or the fake account must not carry a green "Connected" badge | Worker C/D |

## 6. Limits — what could NOT be verified here

- **No real channels / keys / Windows.** No OAuth connect, no real YouTube
  ingest (yt-dlp missing; egress proxy TLS MITM blocks YouTube fetch — same
  as v0.8.1), no Whisper (`faster-whisper` not installed, no
  `OPENAI_API_KEY` — local jobs without `transcript_text` block at
  transcribe with `transcription_backend_missing`), no Gemini/OpenAI key
  test, no TikTok/Meta audit, no X live calls, no real Windows
  Tauri/updater/Job-Object/NSIS behaviour, no live-stream capture.
- **Notifications end-to-end** (bell → real rows) not verified against the
  real tree (was blocked by A1 during most of the audit).
- **yt-dlp one-click install** not verifiable on this VM (egress proxy
  blocks the GitHub release fetch).
- **`/tmp` is a 512 MB tmpfs and filled to 100% during this audit**
  (largely `/tmp/cf-fix082`, 357 MB, from an earlier session — not mine).
  One `POST /worker/run-next` 500 observed while the disk was full was
  almost certainly the disk (`sqlite3.OperationalError: database or disk
  is full`), **not** a code bug — re-verified working afterwards (200).
  Audit data now lives under `~/workspace/audit-cf/` instead.
- **Another worker is running a server on port 8124** with their own test
  data ("UI check — …" jobs); it was left untouched.
- Scratch server (port 8123, snapshot tree) contains one **local-only shim**
  (`src/clipforge/auth/notifications.py` re-implementation) that exists
  only under `~/workspace/audit-cf/tree/` and was never copied back to the
  real repo.

## Appendix — evidence

- **Test results (at audit time, current tree):**
  - Web unit tests: **78/78 pass** (`bash apps/web/scripts/test-unit.sh`),
    including 7 new tests in `apps/web/lib/batch.test.ts` +
    `apps/web/lib/review.test.ts`.
  - `tsc --noEmit`: clean. `npm run build:static`: exit 0, static export
    OK; the fixed strings were verified present in the built JS chunks
    (pipeline "Run job", review "no post was created", trending 3×
    `jobs/view/?id=`, 0× old `/jobs/` links).
  - Live verification on the REAL server (port 8125): batch with the fixed
    page payload → 202; POST /jobs → 201; `run?wait=false` → 200 queued;
    pause → `paused`; resume → `running`; job → `review` with 2
    candidates; approve → `automation.queue` all `skipped_no_account`,
    `GET /posts` → `[]` (exactly the case the ReviewClient now reports
    honestly).
  - **pytest full suite: NOT green** — 19 failed + 1 error in the latest
    full run (20 failed in the run before, 8 in the first run; the count
    is run-dependent/flaky — Worker B's queue tests behave differently
    run to run). Dominant cause: 15× `TypeError: can't compare
    offset-naive and offset-aware datetimes` in `queue_order_key`
    (Worker B's in-flight queue code, see R1); the rest are downstream
    queue-ordering failures (positions, cancel/pause timing). None of
    the failures are in code Worker A touched — Worker A changed no
    Python file.
  - **Ruff: 1 error + 4 unformatted files** — all in other workers'
    new/in-flight files: F841 unused `b` in
    `tests/unit/test_notifications.py:88`; unformatted
    `src/clipforge/job_queue.py`, `src/clipforge/notifications.py`,
    `src/clipforge/worker.py`, `tests/unit/test_notifications.py`.
- Sample video: `~/workspace/audit-cf/sample.mp4` (60 s, 1280×720, 22 MB);
  transcript `~/workspace/audit-cf/transcript.txt`.
- Live-test jobs (REAL server, `~/workspace/audit-cf/data-real`):
  `c80ae3ddd63d4f47` ("A2 recheck real", run via `wait=false` → `queued`);
  batch `3835645055b1`.
- Live-test jobs (old server, `/tmp/cf-audit-data`): `588654523fb649a7`
  (review, 2 candidates), `e0d7391567264b41` + `da95aaeb8af34182`
  (concurrency pair — both ran in parallel on OLD code), `6c9cd1e0888f4625`
  (auto-run probe, still `pending` after 20 s).
- Live-test jobs (scratch, `/tmp/cf-audit-data2` → `~/workspace/audit-cf/data2`):
  `24000660b5d0427d` (pause/resume → review), `f95db0cd894443ae`
  (cancel mid-render), `c9a7b2bb10114cac` (run-next → review, 2 candidates).
- Server logs: `~/workspace/audit-cf/server5.log` (scratch),
  `~/workspace/audit-cf/server-real2.log` (REAL, port 8125).
- Test commands: `python3 -m pytest tests/ -q`; `ruff check . &&
  ruff format --check .`; `npm run build:static`; `bash
  apps/web/scripts/test-unit.sh`; `tsc`.
