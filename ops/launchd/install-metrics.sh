#!/bin/sh
# Install the weekly impact-dashboard refresh as a user LaunchAgent.
# Needs ~/.config/jira-testplan-bot/metrics.env (see metrics-refresh.sh).
# Idempotent. Undo with ./uninstall-metrics.sh.
set -eu

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL="com.jira-testplan-bot.metrics"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
  "$REPO/ops/launchd/$LABEL.plist.template" > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Loaded $LABEL — refreshes once a week, first weekday hour it can."
echo "Log: $HOME/Library/Logs/jira-testplan-metrics.log"
