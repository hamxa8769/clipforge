# ClipForge Architecture — v0.1.0

Source of truth for the research behind this document: the research report
(`research_notes/social-clipping-automation-tool-research-20261120-1033/report.md`,
§§2–3, 6–7, 9). Where that report flags a conflict, the table below says
`VERIFY_LIVE` — read the value from the platform at runtime, never hard-code
it as settled fact.

## 1. Components

| Component | Location | Responsibility |
|---|---|---|
| API | `apps/api/clipforge_api` | FastAPI: jobs, approvals, SEO variants, publishing, monetization tracker |
| Web UI | `apps/web` | Next.js 15 + TypeScript: review clips, trim, approve, schedule, analytics |
| Ingest | `src/clipforge/ingest` | Upload / own-URL intake, source-ownership declaration, hash + dedupe |
| Transcribe | `src/clipforge/transcribe` | FFmpeg audio extract → Whisper / faster-whisper, word timestamps, cache by source hash |
| Highlights | `src/clipforge/highlights` | Chunked transcript → LLM JSON (word indices) → score 0–100 → dedupe >50% overlap → top-N |
| Reframe | `src/clipforge/reframe` | Shot detection first, then face/person tracking + smoothed crop path, 9:16 |
| Captions | `src/clipforge/captions` | Word timestamps → ASS subtitles, safe zones (top 14% / bottom 35% / 6% sides) |
| Render | `src/clipforge/render` | FFmpeg: crop + scale 1080×1920 + captions + loudness −14 LUFS, H.264 30 fps |
| SEO | `src/clipforge/seo` | Per-platform title / description / hashtags / cover, validated against platform rules |
| Scheduler | `src/clipforge/scheduler` | DB-backed queue, idempotency keys, per-account daily caps, staggered slots |
| Publishing | `src/clipforge/publishing` | One adapter per platform over the official API only; dry-run mode; inbox-draft fallback for TikTok |
| Analytics | `src/clipforge/analytics` | Poll YT Analytics / IG-FB Insights / TikTok Display API; feed retention back into clip picks |
| Monetization | `src/clipforge/monetization` | Threshold progress per platform; Feb 2027 YPP doubling flag; never promises earnings |
| DB | `src/clipforge/db` | SQLAlchemy models, Postgres in production, SQLite for local dev |

## 2. Data flow

```
source (upload / own URL)
  → ingest: ownership declaration + sha256 + store (local / S3 / R2)
  → transcribe: faster-whisper (word timestamps) — cached by source hash,
    never re-transcribed
  → highlights: 9-min transcript windows, 15 s overlap → LLM strict-JSON
    (start/end as WORD INDICES, hook 1–10, score 0–100, hook line, reason)
    → merge, re-rank, dedupe >50% overlap (keep higher score) → top-N
  → review (web UI): human picks / trims; nothing proceeds unreviewed
  → reframe + captions + render: 1080×1920 MP4 per clip
  → seo: 4 platform variants from the same master clip
  → approval queue: human approves per clip × platform
  → scheduler: slot within daily caps, staggered per platform
  → publishing adapter: official API (or TikTok inbox draft pre-audit,
    or manual-post pack: file + caption copy)
  → analytics: views / retention / viewed-vs-swiped → next-clip feedback
```

**Queue design (v0.1.0):** jobs run inline in the API worker for local dev.
Separate queues per stage (download / transcribe / score / encode) with
independent concurrency and retries are the Phase 2 design (BullMQ/Redis or
Celery + Redis); the scheduler is DB-backed from day one — never an
in-process `queue.json`, which is lost on restart.

**Virality score — honest caveat:** the 0–100 score is a *predicted*
triage signal, not a measured one. No vendor publishes predictive accuracy.
ClipForge always shows the score **with** its hook line and reason, and
validates picks against real retention / viewed-vs-swiped data.

## 3. DB schema (v0.1.0)

| Table | Key columns |
|---|---|
| `users` | id, email, created_at |
| `accounts` | id, user_id, platform, external_id, oauth tokens (encrypted at rest), token_expires_at, status (`pending_review` / `connected` / `audit_pending`) |
| `jobs` | id, user_id, source_url / upload_path, source_hash, ownership_declared, status (`queued` → `transcribing` → `scoring` → `review` → `approved` → `scheduled` → `published` / `failed`), created_at |
| `clips` | id, job_id, start_word, end_word, start_s, end_s, score, hook, hook_line, reason, render_path, status |
| `seo_variants` | id, clip_id, platform, title, description, hashtags, cover_s, ai_labelled |
| `approvals` | id, clip_id, platform, approved_by, approved_at, decision (`approved` / `rejected`), note |
| `scheduled_posts` | id, clip_id, platform, account_id, run_at, idempotency_key (unique), status |
| `publish_attempts` | id, scheduled_post_id, adapter, request, response, status, attempted_at |
| `analytics_snapshots` | id, clip_id, platform, views, avg_view_s, completion_pct, viewed_vs_swiped, fetched_at |
| `monetization_snapshots` | id, account_id, platform, subs/followers, watch_hours, views_30d/90d, threshold_progress, fetched_at |

Tokens are encrypted at rest; refresh tokens are rotated per platform rules;
least-privilege OAuth scopes only. See [SECURITY.md](SECURITY.md).

## 4. Queue / scheduler design

- **Idempotency:** every scheduled post carries a unique idempotency key
  (`job_id:clip_id:platform:slot`). Re-delivery / retry with the same key
  is a no-op — a clip is never published twice by accident.
- **Daily caps** are enforced per account per platform (see §6). A post
  that would exceed a cap is deferred to the next day's first slot, not
  dropped, and the UI says so.
- **Staggering:** the same clip goes out at different times per platform
  (e.g. Reels 11:00, Facebook 13:00, Shorts 15:00, TikTok 19:00 local)
  rather than as a simultaneous blast, and each copy is the platform's own
  SEO variant — never the identical file + caption four times.
- **Approval gate:** the scheduler only ever sees `approved` clips. The
  publish endpoint returns 409 for anything not approved.

## 5. Publishing adapters

One adapter per platform, one shared interface: `validate(clip)`,
`publish(clip, variant, dry_run)`, `poll_status(external_id)`. All adapters:

- run in `dry_run` mode by default (log the exact request, call nothing);
- read rate limits live where the platform exposes them;
- never retry a 4xx caused by a policy rejection (audit / review / scope);
- store the platform's returned post id in `publish_attempts`.

| Platform | API | Mode in v0.1.0 |
|---|---|---|
| YouTube Shorts | YouTube Data API v3 `videos.insert` (+ `publishAt` scheduling) | Official API once the Cloud project passes compliance audit; manual-post pack until then |
| Instagram Reels | Graph API 2-step container (`/media` → poll → `/media_publish`) | Official API after App Review + Business Verification |
| Facebook Reels | Graph API `/{page}/video_reels` | Official API after App Review |
| TikTok | Content Posting API — Direct Post + Upload (inbox draft) | **Upload / inbox draft only until the audit passes**; Direct Post is audit-gated and unaudited apps are forced private-only |

## 6. Limits table

Read live where the platform exposes the value; the numbers below are the
research-report values (20 Nov 2026) and must be re-verified before quoting
to users.

| Platform | Daily / rate limit | Format limits | Review gate |
|---|---|---|---|
| YouTube | **VERIFY_LIVE — quota conflict:** older docs 1,600 units/upload ≈ 6/day from the 10,000-unit pool; newer sources say `videos.insert` = 1 unit with a separate 100-uploads/day bucket. Check Cloud Console. | Short = vertical/square file, ≤3 min; custom Shorts thumbnails YPP-only (Studio) | Compliance audit for quota increase / public uploads from an unverified app |
| Instagram | 100 API posts / rolling 24 h (Reels + feed + Stories combined); 200 calls/hr/user. Read `content_publishing_limit` live. | 3 s–15 min, ≤300 MB, ≤1920 px wide, MP4/MOV H.264 + AAC, 23–60 fps; hashtag cap **5** in-app (Dec 2025) | App Review + Business Verification for Advanced Access |
| Facebook | 30 Reels / rolling 24 h per Page | 3–90 s via API (in-app Reels have no length cap), ≥540×960, MP4, 24–60 fps | App Review (+ Business Verification for other people's Pages) |
| TikTok | Direct Post 6 req/min per user token; Upload ≤5 pending drafts/user/24 h; unaudited: Direct Posts forced `SELF_ONLY` (private), ≤5 users/24 h | ≤4 GB, ≤10 min (per-account cap 3/5/10 min — check `creator_info.max_video_post_duration_sec`), MP4/WebM/MOV | **Audit is the real gate** (demo video, ≥1 end-to-end; changes re-reviewed; reported 6 weeks / 12+ submissions) |

## 7. Module contracts (v0.1.0)

The tests in `tests/` are written against these APIs, which are the ones
that actually landed. If an API must change, change the test, the
implementation, and this doc together.

- `clipforge.highlights`
  - `Candidate(start_word_index, end_word_index, start_s, end_s, score,
    hook, hook_line, reason, signals={}, source="heuristic")` —
    boundaries are word indices first; `score` 0–100, `hook` 1–10.
  - `overlap_fraction(a, b) -> float` — TIME overlap / shorter duration,
    0–1.
  - `dedupe_and_rank(candidates, top_n=10) -> list[Candidate]` — drop a
    candidate when its overlap with a higher-scored kept candidate is
    **> `DEDUPE_OVERLAP` (0.5)**; result sorted by score desc.
  - `score_span(words, start, end) -> (score, hook, signals)`;
    `chunk_transcript(transcript, window_s=540, overlap_s=15)`;
    `find_heuristic_candidates(transcript, top_n=10)`.
- `clipforge.seo`
  - `generate_seo(platform, keyword, hook, title_hint="",
    duration_s=30.0, extra_hashtags=None, llm_polish=False) -> dict`
    with keys `platform, title, description, caption, hashtags,
    duration_s, warnings` (`warnings` comes from `validate()`).
  - `generate_all(keyword, hook, ...) -> {platform: variant}` (all four).
  - `validate(platform, variant) -> list[str]` — warnings, `[]` = clean:
    hashtag count vs `max_hashtags`, duration vs API min/max, YouTube
    title ≤100 (aim 55–60), TikTok <60 s → Creator Rewards warning.
  - Limits live in one place: `clipforge.publishing.limits.PLATFORM_LIMITS`
    (`max_hashtags`: youtube 3, instagram 5, tiktok 5, facebook 3;
    `title_max_chars`: youtube 100; `max_duration_s`: youtube 180,
    instagram 900, facebook 90, tiktok 600).
- `clipforge.monetization`
  - `THRESHOLDS: dict[platform, list[{program, metrics, note}]]` — YPP
    Early (500 / 3,000 h / 3M views), YPP Full (1,000 / 4,000 h / 10M),
    YPP Full 2027-doubled (1,000 / 8,000 h / 20M), TikTok Rewards
    (10,000 followers / 100,000 views / 30 d, ≥60 s, personal account),
    Instagram Gifts (500) / Subscriptions (10,000) — no Reels Play
    (ended 31 Aug 2025), Facebook CMP (10,000 followers; legacy 5,000
    figure `VERIFY_LIVE`).
  - `progress(current_metrics, platform=None) -> list[dict]` — one row per
    program: `{platform, program, current, threshold, progress_pct,
    missing, note}`. `*_alt` metrics are OR-paths (Shorts views OR hours).
- `clipforge.scheduler`
  - `DAILY_CAPS` (from `publishing.limits`): `{"youtube": 6,
    "instagram": 100, "facebook": 30, "tiktok": 5}` — YouTube 6 is the
    conservative figure (1,600 units/insert); the newer 100/day bucket is
    `PLATFORM_LIMITS["youtube"]["daily_uploads_new_bucket"]`, `VERIFY_LIVE`.
    TikTok 5 = pending inbox drafts / 24 h pre-audit.
  - `due_posts(session, now=None)`, `published_today_count(session,
    account_id, platform, now=None)`, `daily_cap_for(session, post)`
    (per-account `daily_cap_override` wins), `run_due_posts(session,
    dry_run=True, now=None, publish_fn=None) -> list[dict]` — idempotent
    (published/dry-run posts leave the due set); cap-reached posts are
    left scheduled with an error note, not failed.
  - Idempotency is enforced by the DB: `Post.idempotency_key` is unique
    (`uq_post_idempotency`); a duplicate insert raises `IntegrityError`.
- `clipforge.api` — the FastAPI app is `apps/api/clipforge_api/app.py`,
  imported as `clipforge_api.app`. Endpoints per [docs/api.md](docs/api.md).
  `GET /health` returns `{"status": "ok", "version": "0.1.0",
  "dry_run": bool, "database_url_scheme": str}`.
- `clipforge.pipeline.run_pipeline(source_path, *, youtube_url=None,
  own_content=False, top_n=3, caption_style="bold-pop",
  blurred_background=False, transcript_text=None, transcribe_engine=None,
  use_llm=None, out_dir=None, data_dir=None) -> dict` with keys
  `job_id, source, transcript, candidates, clips, rendered_count,
  out_dir`. Each clip entry: `{rank, candidate, status, render?}` where
  `render` is `{path, width, height, duration_s, video_codec,
  size_bytes}` (verified 1080×1920 by `render.ffprobe_verify`). No
  top-level `output_path` key. `scripts/smoke_test.py` uses
  `transcript_text` + `transcribe_engine="fallback-text"` so no Whisper
  backend is needed for a smoke run.

## 8. Reuse / references

- **Reuse (MIT):** `zhouxiaoka/autoclip` — clipping engine / publish-pack
  ideas; `Anil-matcha/AI-Youtube-Shorts-Generator` — highlight-prompt and
  virality-framework design. See [README.md](README.md#reuse-attribution).
- **Reference only (do not copy code):** `gitroomhq/postiz-app` (AGPL-3.0 —
  scheduler/adapter design), `freespirits/social-auto-engine` (MIT —
  approval-queue pattern), `artbyjazi/autoclip` (MIT — architecture,
  word-index highlights, shot-detection-before-crop).
- **Do not use:** `makiisthenes/TiktokAutoUploader` (unofficial TikTok
  web-API uploader, AGPL), any Selenium/cookie uploader, any no-licence
  repo (e.g. `satyamsen7/Reels-Shorts-Scheduler`).
