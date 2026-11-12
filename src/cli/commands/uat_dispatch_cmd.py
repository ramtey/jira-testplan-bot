"""uat-dispatch command — the scheduled side of unattended UAT runs.

One tick: claim a due run from the backend (which recovers dead runs, cancels
ones whose ticket moved, and routes the next round), run its pending legs, then
alert on whatever needs a person. Run by launchd every few minutes inside the
working window (ops/launchd); safe to run by hand.

A file lock keeps two ticks from overlapping: the device is shared, and a
second tick must not see the first one's run as abandoned.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from pathlib import Path

import typer
from typing_extensions import Annotated

from ...app import watch_schedule
from ...app.config import settings
from ...app.services.queue_watcher import notify
from ...app.services.uat_runs import RunRefused, load_profiles
from .uat_run_cmd import _get, _post, _summary, console, run_pending_legs

STATE_DIR = Path(os.environ.get("UAT_DISPATCH_STATE_DIR", Path.home() / ".jira-testplan-bot"))
LOCK_PATH = STATE_DIR / "uat-dispatch.lock"
DAILY_ALERT_PATH = STATE_DIR / "uat-daily-budget.json"



def _notify_hourly(kind: str, title: str, message: str) -> None:
    """A machine-level alert (no run to hang it on), at most once an hour."""
    path = STATE_DIR / f"uat-alert-{kind}.txt"
    hour = time.strftime("%Y-%m-%d %H")
    try:
        if path.read_text() == hour:
            return
    except OSError:
        pass
    notify(title, message)
    path.write_text(hour)


def _alert_once(run_id: int, kind: str, title: str, message: str) -> None:
    try:
        if _post(f"/uat-runs/{run_id}/alerts/{kind}", {}).get("new"):
            notify(title, message)
    except typer.Exit:
        notify(title, message)


def _alert_for(view: dict) -> None:
    """Tell a person about the state a run is left in, once per state.

    No "run ready" alert: the counts it showed were often all not-run, and the
    review is visible in the app anyway. Only budget problems interrupt.
    """
    key, rid = view["ticket_key"], view["run_id"]
    for name, leg in (view.get("legs") or {}).items():
        if leg.get("budget_alert"):
            _alert_once(rid, f"budget_{name}", f"UAT budget: {key}", leg["budget_alert"])


def _alert_events(events: list[dict]) -> None:
    for e in events:
        key, rid, kind, detail = e["ticket_key"], e["run_id"], e["event"], e.get("detail") or ""
        if kind == "awaiting_review":
            _alert_for(_get(f"/uat-runs/{rid}"))
        elif kind == "still_waiting":
            view = _get(f"/uat-runs/{rid}/review")
            started = view.get("created_at")
            if started and _hours_since(started) >= settings.uat_build_alert_hours:
                _alert_once(rid, "build_wait", f"UAT waiting on a deploy: {key}", detail)
        elif kind in ("cancelled", "lease_expired"):
            console.print(f"  run {rid} {key}: {kind} — {detail}")
            if kind == "lease_expired":
                _alert_once(rid, "lease_expired", f"UAT run interrupted: {key}", detail)
        elif kind == "error":
            console.print(f"  [red]run {rid} {key}: {detail}[/red]")
            _alert_once(rid, f"error_{int(time.time() // 3600)}", f"UAT run blocked: {key}", detail)
        else:
            console.print(f"  run {rid} {key}: {kind} — {detail}")


def _hours_since(iso: str) -> float:
    from datetime import datetime, timezone
    then = datetime.fromisoformat(iso)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


def _daily_budget(answer: dict) -> None:
    spent, budget = float(answer.get("cost_today_usd") or 0), float(answer.get("daily_budget_usd") or 0)
    if not budget or spent <= budget:
        return
    today = time.strftime("%Y-%m-%d")
    try:
        state = json.loads(DAILY_ALERT_PATH.read_text())
    except (OSError, ValueError):
        state = {}
    if state.get("alerted") == today:
        return
    notify("UAT daily budget", f"Runs started today cost ${spent:.2f}, over the ${budget:.2f} budget. "
           "Runs continue; this is an alert only.")
    DAILY_ALERT_PATH.write_text(json.dumps({"alerted": today}))


def _tick() -> tuple[str, int | None]:
    holder = {"holder": f"uat-dispatch@{socket.gethostname()}", "host": socket.gethostname(),
              "pid": os.getpid()}
    answer = _post("/uat-dispatch/claim", holder, timeout=300)
    _alert_events(answer.get("events") or [])
    run = answer.get("run")
    if run is None:
        console.print(f"  nothing to run: {answer.get('reason')}")
        return "idle", None
    console.print(f"Run {run['run_id']} {run['ticket_key']}: round with legs "
                  f"{', '.join(n for n, l in run['legs'].items() if l.get('status') == 'pending')}")
    try:
        profiles = load_profiles()
    except RunRefused as e:
        for name, leg in run["legs"].items():
            if leg.get("status") == "pending":
                _post(f"/uat-runs/{run['run_id']}/legs/{name}/finalize",
                      {"exit_code": -1, "missing_code": "blocked_environment", "detail": e.message})
        raise
    try:
        view = run_pending_legs(run, profiles)
    finally:
        _post(f"/uat-dispatch/release/{run['run_id']}", {})
    _summary(view)
    _alert_for(view)
    return "ran", run["run_id"]


def uat_dispatch(
    once: Annotated[bool, typer.Option("--once", help="One tick, then exit (what launchd runs)")] = True,
    ignore_schedule: Annotated[bool, typer.Option("--ignore-schedule", help="Run outside WATCH_ACTIVE_DAYS/HOURS")] = False,
):
    """
    Claim one due UAT run and run its pending legs; alert on what needs a person.

    Examples:
        testplan uat-dispatch --once
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        console.print("another uat-dispatch tick is still running; leaving it be")
        return
    if not ignore_schedule and not watch_schedule.is_active():
        console.print(f"outside the working window ({watch_schedule.describe()}); nothing dispatched")
        return
    outcome, run_id, error = "error", None, None
    try:
        outcome, run_id = _tick()
    except typer.Exit:
        error = "the backend refused or was unreachable; see the log"
        raise
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        _notify_hourly("failed", "UAT dispatcher failed", error[:200])
        raise
    finally:
        try:
            answer = _post("/uat-dispatch/heartbeat", {"outcome": outcome, "run_id": run_id, "error": error})
            _daily_budget(answer)
        except typer.Exit:
            _notify_hourly("backend", "UAT dispatcher",
                           "could not reach the backend; no run can start until it is back")
