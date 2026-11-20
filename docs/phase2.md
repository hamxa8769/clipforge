# ClipForge — Phase 2 features

Implemented on branch `backend/phase2-features` (after v0.1.0). Everything
below works **without API keys, audits, or accounts**: no live YouTube /
Meta / TikTok calls, no Whisper/LLM live paths, no Stripe billing. What is
still blocked — and why — is listed at the end, not hidden.

## 1. Bulk / CSV scheduling

`POST /posts/bulk/csv` (multipart file upload, `dry_run` query param,
default `true`).

Columns (case-insensitive headers; `clip` = `clip_candidate_id`,
`video` = `video_path`, `scheduled_at` = `scheduled_for`):

| Column | Required | Notes |
| --- | --- | --- |
| `clip_candidate_id` | yes | must exist and be `approved` (approval-first is preserved) |
| `platform` | yes | youtube \| instagram \| facebook \| tiktok |
| `scheduled_for` | yes | ISO 8601 datetime; naive values read as UTC |
| `title_override` | no | replaces the SEO title for this post |
| `account_id` | no | must exist if given |
| `idempotency_key` | no | derived from clip + platform + time + title when omitted |
| `brand_kit_id`, `title_variant_id` | no | must exist (and match clip/platform for variants) |

- **Dry-run preview** (`dry_run=true`): validates every row, returns
  `created` (would-be rows), `skipped_duplicates`, and `errors` —
  nothing is stored.
- **Import** (`dry_run=false`): valid rows become `Post` rows with
  status `pending_approval` — they still need a human approve before
  the scheduler will publish them. Rows whose idempotency key already
  exists are reported as `skipped_duplicates`; re-importing the same
  file is a no-op.
- **Errors are per-row**: each error carries its CSV row number and a
  message; one bad row never blocks the rest of the file.

Logic: `src/clipforge/csv_import.py` (`parse_csv`, `validate_rows`,
`import_csv`, `derive_idempotency_key`).

## 2. B-roll (licensed / source-only)

Policy: B-roll comes from exactly two places — a segment of the clip's
own source video (`source_video`), or a file the user uploaded
themselves with a rights declaration (`uploaded_licensed`). **URLs and
third-party downloads are rejected** at validation time and never
stored. There is no downloader in this feature on purpose.

- `POST /clips/{candidate_id}/broll` — attach (requires `source_type`,
  `source_path`, `declaration`; optional `source_start_s` /
  `source_end_s`, `clip_start_s` / `clip_end_s`, `position`
  `picture-in-picture | top-right | bottom-right | fullscreen`,
  `width_frac`). Returns the clip-level overlay spec (output position
  in 1080x1920 pixels).
- `GET /clips/{candidate_id}/broll`, `DELETE /clips/{candidate_id}/broll/{asset_id}`.
- Render wiring is real: `render_clip(..., broll=[BrollOverlay(...)])`
  builds a multi-input FFmpeg filter-complex and overlays the B-roll as
  a picture-in-picture cutaway, enabled only between its clip times,
  with captions applied on top. It cannot be combined with
  `blurred_background` (raises `RenderError`). Pure filter builder:
  `clipforge.broll.build_broll_filter`.

## 3. Brand kits / templates

- `GET /brand-kits/templates` — built-in templates (no DB row needed):
  - **bold-pop** — white on black, gold accent, `bold-pop` captions.
  - **clean-minimal** — dark slate on off-white, teal accent, `boxed` captions.
- `POST /brand-kits` (CRUD: `GET /brand-kits`, `GET /brand-kits/{id}`,
  `PUT /brand-kits/{id}`, `DELETE /brand-kits/{id}`) — fields: `name`,
  `primary_colour` / `secondary_colour` / `accent_colour` (`#RRGGBB`),
  `font_name`, `caption_style` (`bold-pop | karaoke-fill | boxed | default`),
  `logo_path` (local path only, never a URL), `intro_text`, `outro_text`.
  `POST /brand-kits` accepts `template` to clone a built-in, then override.
- Applied at post creation: `POST /posts` accepts `brand_kit_id`; the
  kit's intro/outro wrap the SEO description/caption and the kit metadata
  is recorded under `seo.brand_kit` (the title is never rewritten by a
  kit). `clipforge.brandkits.caption_style_for_kit` gives the renderer
  the caption style for a kit.

## 4. A/B title testing (scaffolding)

- `POST /clips/{candidate_id}/title-variants` — create a variant for a
  clip/platform (`title`, `is_control`; one control per clip/platform —
  a new control demotes the old one).
- `GET /clips/{candidate_id}/title-variants?platform=` — comparison table:
  title, control flag, `assigned_posts`, recorded outcomes, `has_outcome`.
- Assignment: `POST /posts` auto-assigns the least-used variant for the
  clip/platform (balanced round-robin by post count); an explicit
  `title_variant_id` wins. `POST /posts/{id}/title-variant/{variant_id}`
  re-assigns (validates clip/platform match) and rewrites the post's SEO title.
- `POST /title-variants/{id}/outcome` — record `views`,
  `avg_view_duration_s`, `completion_pct`, `viewed_pct` when analytics
  arrive. Values are stored only; nothing fetches them from a platform.
- There is **no automatic winner picking and no automatic re-posting** —
  a human reads the comparison and decides. Frontend work is separate;
  the API + data model here are the contract for it.

## 5. Retention analytics → next-clip feedback

- `POST /analytics/retention` — ingest one retention observation:
  `platform`, `clip_duration_s`, `avg_view_duration_s`,
  `completion_pct`, `viewed_pct`, `dropoff_second`, `views`,
  optional `post_id` / `clip_candidate_id`, `source` (default `manual`).
  `GET /analytics/retention?platform=&clip_candidate_id=&post_id=`.
- `GET /analytics/feedback?platform=` — aggregate feedback from stored
  points. `POST /analytics/feedback/preview` — apply feedback to a
  candidate batch without storing anything.
- Feedback rules (transparent, bounded; see `src/clipforge/retention.py`):
  - Drop-off at ≤3 s in ≥half the points → hook penalty up to 15 pts,
    scaled by the early-drop-off rate; hook-heavy candidates lose most.
  - Mean completion <50% → recommend shortening next clips towards the
    mean viewed duration (floored at 15 s); over-long candidates lose points.
  - Mean completion ≥70% and viewed% ≥70% → no penalty; reuse the pattern.
  - With per-clip `signals`, formats that retained viewers (completion
    ≥70%) earn a signal boost (capped at 10 pts per signal).
  - Base scores are never mutated — the response reports
    `adjusted_score` + `adjustment` + `adjustment_reason` next to them.
- **Real analytics feed: not wired.** The numbers in
  `retention_points` arrive manually or from a platform export. The
  live ingest stubs in `clipforge.analytics` still return
  `not_configured` without credentials. The feedback loop works on
  whatever points are stored, so it is testable today with synthetic
  data and becomes live automatically once a feed is wired.

## 6. Face tracking on a real talking-head video

See `docs/face-tracking-test.md` (and `docs/face-tracking-test.json`).
Short version, reported honestly: **tracking did not engage** — on the
synthetic talking-head video, on the still control, and on the existing
`sample_60s.mp4`, the Haar cascade found 0 faces in every frame, so the
designed centre-crop fallback ran (valid 9:16 plan, crop-x range 0 px).
A follow-up probe with tuned detector settings (scaleFactor 1.05/1.1/1.2,
minNeighbors 3/5, histogram equalisation, `alt`/`alt2`/`profileface`
cascades) also found 0 faces: the cascade is trained on real faces and
a drawn cartoon face does not trigger it. The synthetic fixture
validates the fallback contract, not tracking engagement. Real-human
face tracking remains unverified — no real talking-head asset exists in
this repo and none was downloaded. Two environment notes: the system
Python has no `cv2` at all (reframe falls back by design), and OpenCV
5.x headless wheels ship no `CascadeClassifier` (only 4.10 does);
deployments should pin `opencv-python-headless==4.10.*` until the
reframe moves to a DNN detector.

Reproduce: `scripts/make_talking_head_video.py` (needs cv2) then
`scripts/face_tracking_test.py --videos <mp4s>`.

## API summary (new endpoints)

| Method | Path |
| --- | --- |
| `POST` | `/posts/bulk/csv?dry_run=true` |
| `GET` | `/brand-kits/templates` |
| `POST`/`GET` | `/brand-kits` |
| `GET`/`PUT`/`DELETE` | `/brand-kits/{id}` |
| `POST`/`GET` | `/clips/{id}/title-variants` |
| `POST` | `/posts/{id}/title-variant/{variant_id}` |
| `POST` | `/title-variants/{id}/outcome` |
| `POST`/`GET` | `/clips/{id}/broll` |
| `DELETE` | `/clips/{id}/broll/{asset_id}` |
| `POST`/`GET` | `/analytics/retention` |
| `GET` | `/analytics/feedback` |
| `POST` | `/analytics/feedback/preview` |

`POST /posts` also accepts `brand_kit_id` and `title_variant_id`.
Route note: `GET /analytics/{account_id}` was moved after the new
`/analytics/retention` and `/analytics/feedback` routes, because
FastAPI matches in registration order and the `{account_id}` route
would otherwise swallow them.

## Still blocked (not done, not faked)

- **Live YouTube / Meta / TikTok uploads, TikTok Direct Post, FB Reels
  live posting** — need API keys / OAuth apps / TikTok audit. Nothing
  here calls a platform.
- **Whisper / LLM live paths** — no keys; heuristic + deterministic
  templates only.
- **Real analytics feeds** (YouTube Analytics, IG/FB Insights, TikTok
  Display API) — need credentials; retention ingest is manual/export only.
- **Stripe billing** — not started.
- **Real-human face tracking** — needs a real talking-head recording;
  synthetic test shows the fallback works, engagement is unverified.
- **Frontend** — web UI for these features is separate work; the API
  responses above are the contract.
