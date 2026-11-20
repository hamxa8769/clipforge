#!/bin/sh
# ClipForge web UI (Next.js standalone build) — needs Node.js 20+.
# The API server must be running too (use start-server.sh).
cd "$(dirname "$0")" || exit 1
if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is not installed. The web UI needs Node.js 20+: https://nodejs.org" >&2
  echo "The API server itself does NOT need Node — use start-server.sh." >&2
  exit 1
fi
if [ ! -f web/server.js ]; then
  echo "The web UI build is missing from this folder (web/server.js not found)." >&2
  exit 1
fi
PORT="${PORT:-3000}"
export PORT
echo "Starting ClipForge web UI on http://localhost:${PORT} ..."
echo "(Keep the API server running — start-server.sh)"
exec node web/server.js
