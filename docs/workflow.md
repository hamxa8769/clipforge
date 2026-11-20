# Live workflow — how to exercise it locally

ClipForge v0.3.0 turns the pipeline into a visible live workflow:
**Ingest → Transcribe → Score → Render → SEO → Review → Approve →
Schedule → Publish**. This page walks through it with `curl` against a
local API server. Everything below was verified live against the API
(no platform credentials, no network, FFmpeg only). Where a step
cannot run for real, the API says so — it never fakes success.

## 0. Start the server

```bash
git clone https://github.com/hamxa8769/clipforge.git
cd clipforge
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # includes cryptography (auth/token store)
cp .env.example .env            # dry-run works with no keys at all

cd apps/api
PYTHONPATH=src uvicorn clipforge_api.app:app --reload --port 8000
# API docs: http://127.0.0.1:8000/docs   Health: /health (dry_run: true)
```

The commands below assume `B=http://127.0.0.1:8000`. The server uses
`./data/clipforge.db` (SQLite) unless `DATABASE_URL` /
`CLIPFORGE_DATA_DIR` are set.

## 1. Make a test video (no real footage needed)

```bash
ffmpeg -y -f lavfi -i testsrc=duration=40:size=320x240:rate=10 \
  -c:v libx264 -pix_fmt yuv420p -r 10 /tmp/source.mp4
```

## 2. Upload + run a job

`transcript_text` is the demo fallback: with no faster-whisper and no
`OPENAI_API_KEY`, transcription uses the supplied text spread over the
video duration. The job is flagged `"demo": true` — it is never
presented as a real Whisper transcript.

```bash
B=http://127.0.0.1:8000
JID=$(curl -s -F "file=@/tmp/source.mp4" \
  -F "title=Workflow demo" -F "own_content=true" \
  -F "transcript_text=Stop! This secret is shocking, and nobody tells you the truth. Imagine this: everyone makes the same mistake with short videos, and it is honestly the worst mistake a creator can make. Here is what I discovered after my channel failed three times. The truth is brutal but simple. Most creators are wrong about hooks. They think a hook is a trick, but actually a hook is a promise, and if you break the promise, viewers feel betrayed and swipe away in seconds. That is why your retention is terrible. Listen: here is how to fix it, step by step. First, say the shocking thing first. Second, prove it fast with a real example. Third, never waste the first three seconds on an intro. Do this and watch your videos explode. I tried this exact guide on ten videos, and the results were incredible, unbelievable, amazing. The best part? It is free, it works on every platform, and you can learn it today. Try it now." \
  -F "top_n=3" $B/jobs/upload | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "job=$JID"

# Run it (synchronous; ~seconds for a 40 s test video)
curl -s -X POST $B/jobs/$JID/run | python3 -m json.tool
# -> {"status": "review", "stage": "review", "progress_pct": 95.0, "demo": true, ...}
```

The transcript above scores 66.8 ("good") and 50.8 ("okay") — two
qualifying clips. A weaker transcript scores below the
`MIN_SCORE_THRESHOLD` (50) and produces **alternates** instead of
clips: `GET /jobs/{id}` shows them under `alternates` with their score
and hook line, and the job still reaches `review` with zero clips.
Nothing is faked into a clip.

## 3. Watch the job live

```bash
curl -s $B/jobs/$JID | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d['status'], d['stage'], d['progress_pct'], 'demo:', d['demo'])
for s in d['stages']:
    print(' ', s['stage'], s['status'], s['progress_pct'])
print('failed_stage:', d['failed_stage'], 'duration_s:', d['duration_s'])
print('worker_runs:', d['worker_runs'])
"
# review review 95.0 demo: True
#   ingest done 10.0 / transcribe done 40.0 / score done 55.0
#   render done 90.0 / seo done 95.0 / review ...

# Per-stage log
curl -s $B/jobs/$JID/logs | python3 -m json.tool
# {"logs": [{"ts": ..., "stage": "score", "level": "info",
#            "message": "scored 2 candidates (2 qualifying)"}, ...]}
```

`eta_s` is the average of the last 20 finished worker runs (`null`
with no history yet). `blocked_by` is `null` here; see §6 for a
blocked job.

## 4. Review, approve, watch the automation work

```bash
curl -s $B/jobs/$JID/candidates | python3 -m json.tool
# [{"n": 1, "score": 66.8, "score_band": "good", "status": "pending",
#   "orig_start_s": 0.0, "orig_end_s": 31.5, ...}]

curl -s -X POST $B/jobs/$JID/candidates/1/approve | python3 -m json.tool
# {"id": ..., "n": 1, "status": "approved",
#  "automation": {"seo": {"platforms": ["facebook", "instagram",
#                "tiktok", "youtube"]},
#  "queue": [{"platform": "youtube", "status": "skipped_no_account",
#             "reason": "no connected youtube channel (status: not_connected)"}, ...]}}
```

Approving a clip runs two rules automatically:

- **auto-SEO** — per-platform variants are stored at once:
  `curl -s $B/clips/<candidate_id>/seo` →
  `{candidate_id, seo: {youtube: {...}, instagram: {...}, ...}}`.
- **auto-queue** — one `pending_approval` post per *connected*
  channel, respecting the daily caps. With no channel connected (the
  default here), every platform is **skipped** and the skip is logged
  — the clip is never silently queued to nowhere.

Connect a channel to see a real queue: set a legacy env token before
starting the server (`YOUTUBE_REFRESH_TOKEN=...`) or connect via
OAuth (`GET /channels`, `GET /auth/{platform}/start`). A bare
`SocialAccount` row (`POST /accounts`) does **not** connect a channel
— `GET /channels` is canonical; the row only links the post and
carries the cap override.

Every automation action lands in the feed:

```bash
curl -s "$B/automation/feed?job_id=$JID" | python3 -m json.tool
# [{"kind": "auto_seo_on_approve", "action": "seo_generated", ...},
#  {"kind": "auto_queue_approved", "action": "queue_skipped_no_account", ...}]
```

## 5. Rules, queue, scheduler

```bash
# Rules (server-side; the frontend toggles bind to these)
curl -s $B/automation/rules | python3 -m json.tool
# {"rules": [{"key": "retention_feedback", "kind": "auto_pick_next_clip",
#             "enabled": true, ...}, ...]}

# Turn the high-score auto-approve OFF by default (score >= 95 + hook >= 9)
# — it approves the clip only; post approval before publishing still applies.
curl -s -X PUT $B/automation/rules \
  -H 'Content-Type: application/json' \
  -d '{"key": "auto_approve_high_score", "enabled": false}'

# Next clip for a job, with the retention adjustment explained
curl -s -X POST $B/automation/pick-next/$JID | python3 -m json.tool

# One automation pass over all jobs
curl -s -X POST $B/automation/run | python3 -m json.tool

# The worker queue: running, queued, next, recent runs with durations
curl -s $B/worker/queue | python3 -m json.tool
curl -s -X POST $B/worker/run-next | python3 -m json.tool

# Scheduler state (truthful: every POST /scheduler/run records a run)
curl -s $B/scheduler/status | python3 -m json.tool
# {"running": false, "last_run_at": null, "next_run_at": null,
#  "due_now": 0, "last_error": null, "last_run": null}
# next_run_at is null: the scheduler runs on demand only, no cron.

# Run due posts now (dry-run default; nothing publishes live)
curl -s -X POST "$B/scheduler/run?dry_run=true" | python3 -m json.tool
```

The worker also runs from the CLI (same engine, same tables):

```bash
python3 -m clipforge.worker run <job_id>   # one job
python3 -m clipforge.worker run-next       # oldest queued job
python3 -m clipforge.worker queue          # queue summary
# Options --database-url / --data-dir come BEFORE the subcommand.
```

## 6. Blocked jobs: missing key/tool, named exactly

A YouTube job without yt-dlp installed:

```bash
curl -s -X POST $B/jobs -H 'Content-Type: application/json' -d '{
  "source_type": "youtube", "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "title": "YT demo", "own_content_declaration": true}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])"
# (run it the same way as §2)
curl -s $B/jobs/<id> | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d['status'], d['blocked_by'])"
# blocked {'code': 'yt_dlp_missing', 'dependency': 'yt-dlp', 'kind': 'tool',
#          'detail': '...', 'required_action': 'Install yt-dlp ...'}
```

`blocked` means "retryable once the dependency exists"; a
missing/invalid source file is `failed` instead. Retry keeps the same
job id:

```bash
curl -s -X POST $B/jobs/<id>/retry | python3 -m json.tool   # 200, same job
curl -s -X POST $B/jobs/<id>/cancel | python3 -m json.tool  # 200, cancelled
```

Machine + key inventory (presence only, never values):

```bash
curl -s $B/dependencies | python3 -m json.tool
# {"dependencies": [{"name": "ffmpeg", "kind": "tool", "present": true, ...},
#                   {"name": "OPENAI_API_KEY", "kind": "api_key", "present": false,
#                    "required_action": "Set OPENAI_API_KEY ..."}], "blocked": [...]}
```

## 7. What is still not live

- Real Whisper / LLM paths: no faster-whisper and no `OPENAI_API_KEY`
  in a stock checkout, so transcription above is the flagged demo
  fallback. Install faster-whisper or set `OPENAI_API_KEY` for real
  transcripts.
- Live uploads, TikTok Direct Post (needs audit), Stripe billing:
  still blocked without API keys / platform approval (pre-existing
  v0.3.0 limits).
- Face tracking: renders use the OpenCV centre-crop fallback in this
  environment; real face tracking is unverified live.
- The worker runs in-process (synchronous endpoints / CLI); there is
  no out-of-process daemon yet — the `WorkerRun` / queue tables
  already support one.
- Stagger windows are UTC hours (Instagram 11, Facebook 13, YouTube
  15, TikTok 19).
- Dry-run stays the default everywhere; nothing publishes without a
  human approving the post first.

## 8. Tests

```bash
python3 -m pytest tests/integration/test_workflow_api.py -p no:cacheprovider
python3 -m pytest tests/ -p no:cacheprovider   # full suite
python3 -m ruff check src apps/api tests
python3 -m ruff format --check src/clipforge/automation.py src/clipforge/worker.py \
  src/clipforge/workflow.py apps/api/clipforge_api/app.py \
  tests/integration/test_workflow_api.py
```

Workflow tests: 18 in `tests/integration/test_workflow_api.py`
(end-to-end worker run with real FFmpeg render, blocked YouTube job,
retry/cancel, queue, rules, SEO-on-approve, auto-queue caps,
stagger windows, retention pick-next, transcript-cache regression).
