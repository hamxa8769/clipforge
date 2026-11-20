#!/bin/sh
# ClipForge API server — no Python needed.
# API: http://127.0.0.1:8000 (docs at /docs).
cd "$(dirname "$0")" || exit 1
if [ ! -x ./clipforge-server ]; then
  echo "clipforge-server is missing from this folder." >&2
  exit 1
fi
echo "Starting ClipForge API on http://127.0.0.1:8000 ..."
echo "API docs: http://127.0.0.1:8000/docs"
echo "Press Ctrl+C to stop."
exec ./clipforge-server
