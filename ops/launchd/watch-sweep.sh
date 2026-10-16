#!/bin/sh
# One watcher sweep, timestamped for the log. Run by launchd on an interval;
# safe to run by hand.
#
# The `cd` is not cosmetic: settings read MONGODB_URI from `.env`, and
# pydantic-settings resolves that path relative to the working directory.
# Without it the sweep dies with "MONGODB_URI is not set".
set -eu

# launchd hands this /usr/bin:/bin:/usr/sbin:/sbin. Auto-posting a plan runs
# the frontend's formatter under node, which lives in Homebrew's prefix; on
# launchd's PATH every post would fail with "node is not on PATH".
PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"

printf '\n=== %s ===\n' "$(date '+%Y-%m-%d %H:%M:%S')"
exec "$REPO/.venv/bin/testplan" watch --once --quiet
