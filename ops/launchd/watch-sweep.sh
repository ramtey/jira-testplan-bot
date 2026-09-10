#!/bin/sh
# One watcher sweep, timestamped for the log. Run by launchd on an interval;
# safe to run by hand.
#
# The `cd` is not cosmetic: settings read DATABASE_URL from `.env`, and
# pydantic-settings resolves that path relative to the working directory.
# Without it the sweep dies with "DATABASE_URL is not set".
set -eu

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"

printf '\n=== %s ===\n' "$(date '+%Y-%m-%d %H:%M:%S')"
exec "$REPO/.venv/bin/testplan" watch --once --quiet
