# Setup — ClipForge v0.1.0

## Prerequisites

| Tool | Version | Check |
|---|---|---|
| Python | 3.12 (≥3.11 supported) | `python3 --version` |
| FFmpeg + ffprobe | 8.1+ | `ffmpeg -version`, `ffprobe -version` |
| Node.js + npm | 24 / 10+ | `node --version` (web UI only) |
| Git | any recent | `git --version` |

Optional for local transcription: a machine that can run `faster-whisper`
(CPU works, GPU faster). Without it, transcription falls back to the cloud
Whisper API when `OPENAI_API_KEY` is set, or to a heuristic pass with no
transcript.

## Install

```bash
git clone https://github.com/hamxa8769/clipforge.git
cd clipforge

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# Optional extras (install only what the worker needs):
pip install -e ".[transcribe]"  # faster-whisper, local transcription
pip install -e ".[vision]"      # opencv-python-headless, face tracking
pip install -e ".[youtube]"     # google-api-python-client, YouTube adapter

cp .env.example .env
```

## Environment variables

See `.env.example` — every variable is documented there with placeholder
values. The only ones that matter for a first run:

- `DRY_RUN=true` (default) — nothing is ever published while true.
- `DATABASE_URL` — defaults to SQLite at `data/clipforge.db`.
- `STORAGE_BACKEND=local` — uploads/clips under `data/`.

All provider keys (`OPENAI_API_KEY`, `YOUTUBE_*`, `META_*`, `TIKTOK_*`)
are **optional**. Without them ClipForge generates clips + SEO locally and
publishes nothing — which is the correct state until each platform's OAuth
app is approved (see [publishing.md](publishing.md)).

**Never commit `.env`.** It is gitignored; `detect-private-key` in
pre-commit is a second net, not a substitute.

## Run

```bash
# API — http://localhost:8000 (docs at /docs)
cd apps/api && PYTHONPATH=src uvicorn clipforge_api.app:app --reload --port 8000

# Web review UI — http://localhost:3000
cd apps/web && npm ci && npm run dev
```

## Verify the install

```bash
python3 -m pytest tests/ -q        # unit + integration (contract) tests
ruff check . && ruff format --check .
python3 scripts/smoke_test.py      # end-to-end: sample video → pipeline → 1080×1920 check
```

The smoke test needs FFmpeg on `PATH` and a working `clipforge.pipeline`
(see its output — it reports `BLOCKER` clearly if the pipeline module has
not landed yet).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `pip install -e .` fails: no packages found | The `src/clipforge` package has no modules yet — pull latest; CI installs dev deps directly as a fallback |
| `ffmpeg: command not found` | Install FFmpeg 8.x from your OS package manager or https://ffmpeg.org |
| `pytest` collects 0 tests / all skipped | A module the tests need has not landed yet — the skip reason names it; see `tests/conftest.py` |
| API creates `clipforge.db` in the repo | Expected default (`DATABASE_URL=sqlite:///./clipforge.db`); point `DATABASE_URL` elsewhere for dev, or use the test suite's throwaway DB |
| `npm ci` fails in `apps/web` | Check Node 24 (`node --version`); delete `node_modules` and retry |
| Port 8000 / 3000 in use | `uvicorn ... --port 8001` / `npm run dev -- -p 3001` |
