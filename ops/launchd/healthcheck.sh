#!/bin/sh
# One health check of backend + frontend, run by launchd every minute.
#
# Notifies (macOS notification) when something goes down and again when it
# recovers — on state changes only, never every minute. A service must fail
# two checks in a row before it counts as down, so a launchd restart or a
# reload blip doesn't page. Every transition is logged with a timestamp, which
# is the record of when and for how long it was down.
set -u
exec >>"$HOME/Library/Logs/jira-testplan-health.log" 2>&1
STATE_DIR="$HOME/Library/Application Support/jira-testplan-bot"
mkdir -p "$STATE_DIR"
now() { date '+%Y-%m-%d %H:%M:%S'; }
notify() {
  /usr/bin/osascript -e "display notification \"$2\" with title \"Test Plan Bot\" subtitle \"$1\" sound name \"Basso\"" >/dev/null 2>&1 || true
}

# check NAME URL -> sets REASON, returns 0 if healthy
check() {
  code=$(/usr/bin/curl -s -o /tmp/.tpb-health-$$ -w '%{http_code}' --max-time 10 "$2" 2>/dev/null)
  [ "$code" = "200" ] && { rm -f /tmp/.tpb-health-$$; return 0; }
  if [ "$code" = "000" ] || [ -z "$code" ]; then REASON="not responding"
  else REASON="HTTP $code $(head -c 200 /tmp/.tpb-health-$$ 2>/dev/null | tr -d '\n"')"; fi
  rm -f /tmp/.tpb-health-$$
  return 1
}

run() {
  name=$1 url=$2
  f="$STATE_DIR/health-$name"
  prev=$(cat "$f" 2>/dev/null || echo "up 0 -")
  set -- $prev; state=$1 fails=$2 since=$3
  if check "$name" "$url"; then
    if [ "$state" = "down" ]; then
      echo "$(now) RECOVERED $name (was down since $(cat "$f.since" 2>/dev/null))"
      notify "$name is back up" "Down since $(cat "$f.since" 2>/dev/null)"
    fi
    echo "up 0 -" > "$f"
  else
    fails=$((fails + 1))
    if [ "$state" != "down" ] && [ "$fails" -ge 2 ]; then
      now > "$f.since"
      echo "$(now) DOWN $name — $REASON"
      notify "$name is DOWN" "$REASON"
      echo "down $fails -" > "$f"
    else
      echo "$state $fails -" > "$f"
    fi
  fi
}

run backend  http://127.0.0.1:8000/health
run frontend http://127.0.0.1:5173/
