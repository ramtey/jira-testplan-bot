#!/bin/sh
# One UAT dispatcher tick, timestamped for the log. Run by launchd on an
# interval; safe to run by hand.
#
# The `cd` matters: settings read `.env` relative to the working directory.
set -eu

# launchd hands this /usr/bin:/bin:/usr/sbin:/sbin. The runner is `claude`
# (installed under ~/.local/bin by its installer), the results formatter needs
# node (Homebrew), and the simulator needs xcrun (/usr/bin). Without these
# every leg fails with "command not found" and nothing says why.
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"

printf '\n=== %s ===\n' "$(date '+%Y-%m-%d %H:%M:%S')"
exec "$REPO/.venv/bin/testplan" uat-dispatch --once
