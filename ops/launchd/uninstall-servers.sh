#!/bin/sh
# Stop and remove the backend, frontend and health-check LaunchAgents.
set -eu
for l in health backend frontend; do
  LABEL="com.jira-testplan-bot.$l"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
  echo "Removed $LABEL"
done
echo "Logs are left in ~/Library/Logs."
