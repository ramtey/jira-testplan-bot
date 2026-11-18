#!/bin/sh
# Restart the backend and frontend LaunchAgents (e.g. after `uv sync` or
# `npm install`, which --reload / Vite HMR don't pick up).
set -eu
for l in backend frontend; do
  launchctl kickstart -k "gui/$(id -u)/com.jira-testplan-bot.$l"
  echo "Restarted com.jira-testplan-bot.$l"
done
