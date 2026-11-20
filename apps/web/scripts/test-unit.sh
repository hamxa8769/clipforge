#!/usr/bin/env bash
# Unit tests for the web lib (status normalisation, media URLs).
# No test framework dependency: compiles lib/api.ts + lib/api.test.ts with
# the project's TypeScript, then runs them under Node's built-in runner.
set -euo pipefail
cd "$(dirname "$0")/.."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

./node_modules/.bin/tsc lib/api.ts lib/api.test.ts \
  --target es2022 --module nodenext --moduleResolution nodenext \
  --strict --skipLibCheck --outDir "$TMP"

node --test "$TMP/api.test.js"
