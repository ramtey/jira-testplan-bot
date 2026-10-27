#!/bin/sh
# Weekly refresh of the impact dashboard's data files (scripts/impact_metrics.py).
# launchd starts this hourly; it only does work on a weekday between 08:00 and
# 17:59, and only until one refresh has succeeded this week, so a Mac that was
# asleep or off VPN on Monday morning catches up on the next hour it is awake.
#
# Settings live outside the repo, in ~/.config/jira-testplan-bot/metrics.env:
#   METRICS_OUT="/path/to/synced/folder"
#   METRICS_ARGS="--weekly-since 2026-07-13 --monthly-since 2025-01 ..."
# No config file means not set up on this machine: exit quietly.
set -eu

PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH

CONF="$HOME/.config/jira-testplan-bot/metrics.env"
[ -f "$CONF" ] || exit 0
. "$CONF"

day=$(date +%u); hour=$(date +%H)
[ "$day" -le 5 ] && [ "$hour" -ge 8 ] && [ "$hour" -le 17 ] || exit 0

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
PY="$REPO/.venv/bin/python"

# Done already this week? refresh_status.json is written last by every run.
if "$PY" - "$METRICS_OUT/refresh_status.json" <<'PYEOF'
import datetime as dt, json, sys
try:
    s = json.load(open(sys.argv[1]))[0]
except Exception:
    sys.exit(1)
at = dt.datetime.fromisoformat(s["refreshed_at"]).date()
monday = dt.date.today() - dt.timedelta(days=dt.date.today().weekday())
sys.exit(0 if s.get("ok") and at >= monday else 1)
PYEOF
then
  exit 0
fi

printf '\n=== %s ===\n' "$(date '+%Y-%m-%d %H:%M:%S')"
# shellcheck disable=SC2086  # METRICS_ARGS is a list of flags on purpose
if "$PY" scripts/impact_metrics.py --out "$METRICS_OUT" $METRICS_ARGS; then
  exit 0
fi

# Failed: alert at most once a day, the way the watcher does, then let the
# next hourly run try again.
STAMP="$HOME/.config/jira-testplan-bot/metrics-last-alert"
today=$(date +%F)
if [ "$(cat "$STAMP" 2>/dev/null || true)" != "$today" ]; then
  "$PY" -c 'from src.app.services.queue_watcher import notify; notify("Impact dashboard refresh failed", "The weekly metrics refresh failed; it will retry hourly. Log: ~/Library/Logs/jira-testplan-metrics.log")' || true
  echo "$today" > "$STAMP"
fi
exit 1
