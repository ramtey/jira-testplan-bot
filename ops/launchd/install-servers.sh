#!/bin/sh
# Install backend + frontend as always-on LaunchAgents (start at login,
# restart on crash) plus a once-a-minute health check that sends a macOS
# notification when either goes down or comes back.
#
# Idempotent — re-run after editing a template. Undo with ./uninstall-servers.sh.
# Ports 8000 and 5173 must be free: stop any hand-started uvicorn/vite first.
set -eu
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$HOME/Library/LaunchAgents"
for l in backend frontend health; do
  LABEL="com.jira-testplan-bot.$l"
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
    "$REPO/ops/launchd/$LABEL.plist.template" > "$PLIST"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "Loaded $LABEL"
done
cat <<MSG

Backend  http://localhost:8000  log: ~/Library/Logs/jira-testplan-backend.log
Frontend http://localhost:5173  log: ~/Library/Logs/jira-testplan-frontend.log
Outages (down/recovered with times): ~/Library/Logs/jira-testplan-health.log
Restart: $REPO/ops/launchd/restart-servers.sh
MSG
