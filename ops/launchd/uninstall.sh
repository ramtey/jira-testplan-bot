#!/bin/sh
# Stop and remove the watcher LaunchAgent.
set -eu

LABEL="com.jira-testplan-bot.watch"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Removed $LABEL. Sweeps stopped; the log is left in place."
