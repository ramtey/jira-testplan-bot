#!/bin/sh
# Install the watcher as a user LaunchAgent: a sweep every 5 minutes, so a
# plan is waiting before anyone opens the ticket.
#
# Idempotent — re-run it after editing the template. Undo with ./uninstall.sh.
set -eu

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL="com.jira-testplan-bot.watch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
  "$REPO/ops/launchd/$LABEL.plist.template" > "$PLIST"

# bootout first so a re-run picks up template changes instead of silently
# keeping the old schedule.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Loaded $LABEL — sweeps every 5 minutes."
echo "Log:    $HOME/Library/Logs/jira-testplan-watch.log"
echo "Status: launchctl print gui/$(id -u)/$LABEL | head -20"
