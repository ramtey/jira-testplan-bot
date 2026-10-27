#!/bin/sh
# Stop and remove the weekly impact-dashboard refresh.
set -eu
LABEL="com.jira-testplan-bot.metrics"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Removed $LABEL."
