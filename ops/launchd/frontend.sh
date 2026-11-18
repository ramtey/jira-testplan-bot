#!/bin/sh
# Frontend (Vite dev server on :5173), kept alive by launchd. Safe to run by hand.
# Vite hot-reloads on its own. --strictPort: CORS on the backend is pinned to
# 5173, so silently moving to 5174 would look up but fail every API call.
set -eu
PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO/frontend"
printf '\n=== frontend start %s ===\n' "$(date '+%Y-%m-%d %H:%M:%S')"
exec "$REPO/frontend/node_modules/.bin/vite" --port 5173 --strictPort
