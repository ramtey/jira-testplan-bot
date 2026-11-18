#!/bin/sh
# Backend (uvicorn on :8000), kept alive by launchd. Safe to run by hand.
#
# --reload stays on: launchd restarts a crashed process, the reloader picks up
# code changes, so the running backend is never older than the checkout
# (a stale backend has passed for shape drift before). The `cd` matters:
# settings read `.env` relative to the working directory.
set -eu
PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"   # node, for the plan formatter
export PATH
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
printf '\n=== backend start %s ===\n' "$(date '+%Y-%m-%d %H:%M:%S')"
exec "$REPO/.venv/bin/uvicorn" src.app.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir src
