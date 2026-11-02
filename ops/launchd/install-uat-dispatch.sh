#!/bin/sh
# Install the UAT dispatcher as a user LaunchAgent: a tick every 5 minutes
# that runs queued UAT runs unattended. Runs cost money: set UAT_AUTO_QUEUE,
# runner profiles and budgets first (docs/design/uat-loop.md, ops/uat/README.md).
#
# Idempotent — re-run it after editing the template. Undo with
# ./uninstall-uat-dispatch.sh.
set -eu

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL="com.jira-testplan-bot.uat-dispatch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
  "$REPO/ops/launchd/$LABEL.plist.template" > "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Loaded $LABEL — a dispatch tick every 5 minutes."
echo "Log:    $HOME/Library/Logs/jira-testplan-uat-dispatch.log"
echo "Status: launchctl print gui/$(id -u)/$LABEL | head -20"
