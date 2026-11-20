# ClipForge Web — v0.3.0 (frontend)
Next.js 15 + TypeScript + Tailwind review/scheduler dashboard (App Router).

## Pages
- `/` — Dashboard: live stats cards (from `GET /analytics`), channels strip (`GET /accounts`), recent jobs with live stage/progress, approval-queue summary, up-next schedule, monetization vs thresholds (Feb 2027 doubling flag). One primary action: Review approval queue.
- `/automation` — Automation runner: running/queued jobs (10s poll), history with outcome + error, what is next, retention-feedback signals, "Run scheduler now" (`POST /scheduler/run`, dry-run default), rule toggles (browser-local only — see `docs/API-CONTRACT.md`).
- `/channels` — One card per platform: account, connection state, latest analytics snapshot, retention signals, connect guidance.
- `/queue` — Approval queue. `/schedule` — Staggered posting times + post list with state filters (All / Scheduled / Awaiting approval / Published / Failed); failed posts show their stored error inline.
- `/jobs/view/?id=<id>` — Job detail: 8-stage pipeline stepper, progress, error/run log, retry (re-creates the job from the same source), then the review list (video player + timeline trim editor + per-platform SEO).
- `/monetization` — Threshold progress per platform, computed with real metrics from the latest analytics snapshot. `/settings` — Live connected accounts (`GET /accounts`); no key/token field exists in this UI by design.

Design system: `components/ui.tsx` (one dark token set, `Card`/`StatusPill`/`ProgressBar`/`Skeleton`/`EmptyState`/`ErrorBanner`/`Toggle`). Every screen has loading / empty / error states; demo fallback is always badged. UI patterns borrowed vs built fresh: `docs/REUSE.md`. Backend fields the frontend still needs: `docs/API-CONTRACT.md`.

## Run
```bash
npm install
npm run dev      # http://localhost:3000
npm run build    # production build (standalone Node server)
npm run build:static  # pure static export in apps/web/out (no Node at runtime)
npm run lint
```

## Config
Set the API base URL (defaults to http://localhost:8000):
```
NEXT_PUBLIC_API_URL=http://localhost:8000
```
API keys are entered in `.env` on the server only — never in this browser UI.

## Static export (desktop app)
`npm run build:static` builds the UI as plain files in `apps/web/out/`
(equivalent to `CLIPFORGE_STATIC_EXPORT=1 NEXT_PUBLIC_API_URL= next build`;
the env vars are set by `scripts/build-static.mjs` so the script also
works on Windows). `clipforge-server` serves that folder at `/` when it
is staged at `<base>/web/out` next to the executable — see
`apps/api/clipforge_api/__main__.py` and
`python scripts/build_exe.py --with-web-static`. The UI then calls the
API same-origin (relative `/jobs`, …) on whatever port the server runs
on, so no port is hardcoded: an empty `NEXT_PUBLIC_API_URL` at build
time means "same origin", while an unset one keeps the dev default
`http://localhost:8000` (see `resolveApiUrl` in `lib/api.ts`).

All pages fetch their data in the browser (`useEffect`), falling back to
clearly labelled **Demo data** when the API is unreachable — nothing is
fetched at build time. One limit: `/jobs/<id>` is a dynamic route, and a
static export can only generate pages for ids known at build time, so
`generateStaticParams()` lists the demo job ids (`job-001`–`job-004`). A
live job id not known at build time has no exported HTML file, so
opening `/jobs/<live-id>` directly (or navigating to it from the
dashboard) returns 404 from the static file server. Live jobs are
reviewed through `/jobs/view/?id=<id>` instead — a single static page
(`jobs/view/index.html`) that reads the id from the query string in the
browser and renders the same review UI; in-app links point there.
Every other page (`/`, `/automation`, `/channels`, `/queue`, `/schedule`, `/monetization`,
`/settings`) is fully static.

## Scaffold note
Hand-rolled minimal Next.js scaffold (package.json + next.config + app/ layout) rather than `npx create-next-app`, for speed and reliability in this VM. Same stack (Next 15, React 19, Tailwind 3, TypeScript) and same App Router structure; no functionality lost.

## Data
When the FastAPI backend at `NEXT_PUBLIC_API_URL` is unreachable, pages fall back to clearly labelled **Demo data** (mock). Never treat demo data as live.
