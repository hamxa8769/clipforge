# ClipForge Web — v0.1.0
Next.js 15 + TypeScript + Tailwind review/scheduler dashboard (App Router).

## Run
```bash
npm install
npm run dev      # http://localhost:3000
npm run build    # production build
npm run lint
```

## Config
Set the API base URL (defaults to http://localhost:8000):
```
NEXT_PUBLIC_API_URL=http://localhost:8000
```
API keys are entered in `.env` on the server only — never in this browser UI.

## Scaffold note
Hand-rolled minimal Next.js scaffold (package.json + next.config + app/ layout) rather than `npx create-next-app`, for speed and reliability in this VM. Same stack (Next 15, React 19, Tailwind 3, TypeScript) and same App Router structure; no functionality lost.

## Data
When the FastAPI backend at `NEXT_PUBLIC_API_URL` is unreachable, pages fall back to clearly labelled **Demo data** (mock). Never treat demo data as live.
