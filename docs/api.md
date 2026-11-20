# API Reference — ClipForge v0.1.0

Base URL (local dev): `http://localhost:8000`. Interactive docs: `/docs`.
The app is `apps/api/clipforge_api/app.py` (imported as
`clipforge_api.app`); run it from `apps/api` with
`PYTHONPATH=src uvicorn clipforge_api.app:app --port 8000`.

Auth in v0.1.0: none on local dev (single-user). API-key auth is planned
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
{ "status": "ok", "dry_run": true, "version": "0.1.0", "database_url_scheme": "sqlite" }
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

### `GET /analytics` · `GET /analytics/{account_id}`

Retention / viewed-vs-swiped signals feeding the next clip picks.

### `POST /scheduler/run`

Run due posts now (dry-run by default). Same engine the scheduler loop
uses: caps, idempotency, backoff.

## Errors

| Status | Meaning |
|---|---|
| 400 | Malformed request / unknown platform |
| 404 | Unknown job / candidate / post / account |
| 409 | Approval-first violation (unapproved candidate or post); duplicate idempotency key; live publishing disabled while dry-run is on |
| 422 | Missing ownership declaration; missing source; trim outside the candidate span; invalid platform value |
| 502 | Platform adapter error (post marked `failed`, error stored on the post) |

## Dry-run semantics

- `dry_run=true` (default on publish, and `CLIPFORGE_DRY_RUN=true` in the
  environment): adapters validate everything and return the exact payload
  they *would* send. No token is used, no post is created.
- `dry_run=false`: only meaningful with real, approved OAuth tokens,
  approved posts, and `CLIPFORGE_DRY_RUN=false`. Flip it per environment,
  never per request, and never in a test.
