ClipForge v0.1.0 — packaged release
=================================

AI short-form clipping + per-platform SEO + approval-first publishing for
YouTube Shorts, Facebook Reels, Instagram Reels, and TikTok.

This zip needs NO Python. It contains:

  clipforge-server(.exe)   One-file API server. Double-click start-server
                           to run it. No Python installation needed.
  web/                     The web review UI (Next.js standalone build).
                           Needs Node.js 20+ to run (see start-web).
  start-server.bat/.sh     Start the API server (http://127.0.0.1:8000)
  start-web.bat/.sh        Start the web UI (http://localhost:3000)
  .env.example             Copy to .env and fill in what you need.
  README.txt               This file.

QUICK START
-----------
1. Unzip this folder anywhere.
2. Start the API server:
     Windows: double-click start-server.bat
     Linux:   ./start-server.sh
   It prints its address: http://127.0.0.1:8000 (API docs at /docs).
3. Start the web review UI (needs Node.js 20+ installed separately):
     Windows: double-click start-web.bat
     Linux:   ./start-web.sh
   Then open http://localhost:3000 in your browser.
   The web UI calls the API on port 8000, so keep the API server running.
4. First run creates a data/ folder next to the exe with clipforge.db
   (SQLite). Your jobs, clips, and approvals live there.

PREREQUISITES
-------------
* FFmpeg must be installed separately and on PATH — the exe does NOT
  bundle it. Clipping/transcription/rendering will fail without it.
  Windows: https://ffmpeg.org/download.html (or `choco install ffmpeg`)
  Linux:   sudo apt install ffmpeg
* Node.js 20+ — only for the web UI (start-web). The API server does
  not need it. https://nodejs.org

CONFIGURATION
-------------
* Copy .env.example to .env in this folder to set options (database,
  dry-run, API keys). The exe reads .env from its own folder.
* DRY_RUN=true is the default: nothing publishes live. Publishing needs
  a human approve step in the web UI first, real platform credentials,
  and DRY_RUN=false. See docs/publishing.md in the repo.
* Ports: CLIPFORGE_API_PORT (default 8000) for the API,
  PORT=3000 for the web UI (set before running start-web).

WHAT THE EXE DOES NOT DO
------------------------
* It does not serve the web UI by itself: the Next.js app is a
  server-rendered app and needs Node. If you place a static export of
  the web app at web/out/ next to the exe, the exe WILL serve it at
  http://127.0.0.1:8000/ instead of the API-only status page.
* It never posts to any platform on its own. Every post waits for a
  human approve, and dry-run is the default.

SOURCE / ISSUES
---------------
https://github.com/hamxa8769/clipforge
MIT License — see LICENSE in the repo.
