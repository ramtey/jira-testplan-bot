#!/bin/sh
# Stop and remove the UAT dispatcher LaunchAgent. Queued runs stay queued.
set -eu

LABEL="com.jira-testplan-bot.uat-dispatch"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Removed $LABEL. No run starts unattended; the log is left in place."
