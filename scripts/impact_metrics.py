"""Refresh every data file behind the impact dashboard in one go.

Writes into ``--out`` (meant to be a synced Google Drive folder the dashboard
reads live):

    jira_weekly.json       tickets entering testing, QA bounces, bugs filed — per week
    jira_monthly.json      the same per calendar month (the before-the-bot baseline)
    plan_wait_tickets.csv  Ready-to-Test -> first bot plan, per ticket (plan_wait_metrics.py)
    ai_tested_weekly.csv   distinct tickets with a uat-runner run folder started per week
    notes_weekly.csv       hand-written notes kept as they are; a week with none gets a
                           drafted line from that week's commits and watcher errors
    refresh_status.json    when this ran and whether every file refreshed

Files are rewritten in place, never deleted and recreated: Drive keeps a file's
id across in-place writes, and the dashboard is pinned to those ids.

Usage:
    uv run python scripts/impact_metrics.py --out "<drive folder>" \
        --weekly-since 2026-07-13 --monthly-since 2025-01 \
        --wait-since 2025-12-01 --split 2026-09-08 --runs-dir ~/uat-walkthroughs

Read-only against Jira, GitHub-free, read-only against Mongo. Exits 1 if any
file failed, after recording which in refresh_status.json.
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import traceback

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.plan_wait_metrics import _first_entry, _first_plans, _jql_keys  # noqa: E402
from src.app.config import settings  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCH_LOG = os.path.expanduser("~/Library/Logs/jira-testplan-watch.log")


def _write(path: str, text: str) -> None:
    # In place on purpose: see the module docstring.
    with open(path, "w", newline="") as f:
        f.write(text)


def _csv(rows: list[dict], fields: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def _monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _count(jira: httpx.Client, jql: str) -> int:
    return len(_jql_keys(jira, jql))


def _period_counts(jira, project, testing, bounce_from, bounce_to, start: dt.date, end: dt.date) -> dict:
    """``end`` is exclusive. DURING is inclusive, so it gets the day before."""
    last = end - dt.timedelta(days=1)
    during = f'DURING ("{start}", "{last}")'
    bounces = " OR ".join(f'status CHANGED FROM "{f}" TO "{t}" {during}' for f in bounce_from for t in bounce_to)
    return {
        "entered_testing": _count(jira, f'project = {project} AND status CHANGED TO "{testing}" {during}'),
        "qa_bounced": _count(jira, f"project = {project} AND ({bounces})"),
        "bugs_filed": _count(jira, f'project = {project} AND issuetype = Bug AND created >= "{start}" AND created < "{end}"'),
    }


def jira_counts(args, today: dt.date) -> tuple[list[dict], list[dict]]:
    with httpx.Client(base_url=settings.jira_url, auth=(settings.jira_username, settings.jira_api_token),
                      timeout=60) as jira:
        common = (args.project, args.testing_status, args.bounce_from, args.bounce_to)
        weekly, w = [], dt.date.fromisoformat(args.weekly_since)
        while w <= today:
            nxt = w + dt.timedelta(days=7)
            weekly.append({"week_start": str(w), **_period_counts(jira, *common, w, min(nxt, today + dt.timedelta(days=1)))})
            w = nxt
        monthly, y, m = [], *map(int, args.monthly_since.split("-"))
        while dt.date(y, m, 1) <= today:
            start = dt.date(y, m, 1)
            y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
            end = dt.date(y2, m2, 1)
            row = {"month": f"{y}-{m:02d}", **_period_counts(jira, *common, start, min(end, today + dt.timedelta(days=1)))}
            if end > today:
                row["partial"] = True
            monthly.append(row)
            y, m = y2, m2
    return weekly, monthly


def plan_wait(args) -> list[dict]:
    since = dt.datetime.fromisoformat(args.wait_since).astimezone()
    split = dt.date.fromisoformat(args.split) if args.split else None
    with httpx.Client(base_url=settings.jira_url, auth=(settings.jira_username, settings.jira_api_token),
                      timeout=60) as jira:
        keys = _jql_keys(jira, f'project = {args.project} AND status CHANGED TO "{args.ready_status}" '
                               f'AFTER "{args.wait_since}"')
        ready = {k: _first_entry(jira, k, args.ready_status, since) for k in keys}
    ready = {k: v for k, v in ready.items() if v is not None}
    plans = _first_plans(set(ready))
    rows = []
    for key, entered in sorted(ready.items(), key=lambda kv: kv[1]):
        plan = plans.get(key)
        day = entered.astimezone().date()
        rows.append({
            "ticket": key,
            "ready_at": entered.astimezone().isoformat(timespec="minutes"),
            "plan_at": plan.astimezone().isoformat(timespec="minutes") if plan else "",
            "wait_hours": "" if plan is None else round(max(0.0, (plan - entered).total_seconds() / 3600), 2),
            "outcome": "no_bot_plan" if plan is None else ("ready_before_queue" if plan <= entered else "after_queue"),
            "week_start": str(_monday(day)),
            "era": "" if split is None else ("after" if day >= split else "before"),
        })
    return rows


def ai_tested(args, today: dt.date) -> list[dict]:
    """Distinct tickets per week with a run folder named sk<number>*, dated by its oldest file."""
    root = os.path.expanduser(args.runs_dir)
    weeks: dict[dt.date, set[str]] = collections.defaultdict(set)
    for name in os.listdir(root):
        m = re.match(r"sk(\d+)", name, re.I)
        path = os.path.join(root, name)
        if not m or not os.path.isdir(path):
            continue
        files = [f for f in os.listdir(path) if not f.startswith(("_", "."))]
        if not files:
            continue
        first = min(os.stat(os.path.join(path, f)).st_mtime for f in files)
        weeks[_monday(dt.date.fromtimestamp(first))].add(m.group(1))
    start = dt.date.fromisoformat(args.weekly_since)
    rows, w = [], start
    while w <= today:
        rows.append({"week_start": str(w), "tickets_ai_tested": len(weeks.get(w, ())),
                     "complete_week": "yes" if w + dt.timedelta(days=7) <= today else "no"})
        w += dt.timedelta(days=7)
    return rows


def _draft_note(week: dt.date) -> str:
    end = week + dt.timedelta(days=7)
    log = subprocess.run(["git", "-C", REPO, "log", f"--since={week}", f"--until={end}", "--pretty=%s"],
                         capture_output=True, text=True).stdout.splitlines()
    errors = 0
    if os.path.exists(WATCH_LOG):
        stamp = None
        for line in open(WATCH_LOG, errors="replace"):
            if line.startswith("=== "):
                try:
                    stamp = dt.date.fromisoformat(line[4:14])
                except ValueError:
                    stamp = None
            elif "Error" in line and stamp and week <= stamp < end and "Traceback" not in line:
                errors += 1
    parts = [f"Draft: {len(log)} commits"]
    if log:
        parts.append("; ".join(log[:2]))
    if errors:
        parts.append(f"{errors} watcher error lines")
    return " · ".join(parts)


def notes(path: str, weeks: list[str], today: dt.date) -> str:
    existing: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            existing = {r["week_start"]: r["note"] for r in csv.DictReader(f)}
    for w in weeks:
        d = dt.date.fromisoformat(w)
        if w not in existing and d + dt.timedelta(days=7) <= today:
            existing[w] = _draft_note(d)
    rows = [{"week_start": w, "note": n} for w, n in sorted(existing.items())]
    return _csv(rows, ["week_start", "note"])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", required=True, help="folder to write the files into")
    p.add_argument("--project", default=(settings.workflow_project_prefixes or [None])[0])
    p.add_argument("--weekly-since", required=True, help="Monday the weekly series starts")
    p.add_argument("--monthly-since", required=True, help="YYYY-MM the monthly series starts")
    p.add_argument("--wait-since", required=True, help="YYYY-MM-DD for plan wait times")
    p.add_argument("--split", help="YYYY-MM-DD splitting plan waits into before/after")
    p.add_argument("--runs-dir", help="folder of uat-runner run folders (skip AI-tested counts if unset)")
    p.add_argument("--ready-status", default="Ready To Test")
    p.add_argument("--testing-status", default="In Testing")
    p.add_argument("--bounce-from", default="In Testing,Ready for UAT", type=lambda s: s.split(","))
    p.add_argument("--bounce-to", default="In Progress,To Do", type=lambda s: s.split(","))
    args = p.parse_args()
    if not args.project:
        sys.exit("No project: pass --project or set WORKFLOW_PROJECT_PREFIXES in .env")

    os.makedirs(args.out, exist_ok=True)
    today = dt.date.today()
    out = lambda name: os.path.join(args.out, name)  # noqa: E731
    files, errors = [], {}

    def step(name, fn):
        try:
            fn()
            files.append(name)
        except Exception as e:  # one broken source must not stop the others refreshing
            errors[name] = f"{type(e).__name__}: {e}"[:300]
            traceback.print_exc()

    def _jira():
        weekly, monthly = jira_counts(args, today)
        _write(out("jira_weekly.json"), json.dumps(weekly, indent=1))
        _write(out("jira_monthly.json"), json.dumps(monthly, indent=1))

    def _wait():
        rows = plan_wait(args)
        _write(out("plan_wait_tickets.csv"), _csv(rows, list(rows[0])))

    def _ai():
        rows = ai_tested(args, today)
        _write(out("ai_tested_weekly.csv"), _csv(rows, ["week_start", "tickets_ai_tested", "complete_week"]))
        _write(out("notes_weekly.csv"), notes(out("notes_weekly.csv"), [r["week_start"] for r in rows], today))

    step("jira_weekly.json + jira_monthly.json", _jira)
    step("plan_wait_tickets.csv", _wait)
    if args.runs_dir:
        step("ai_tested_weekly.csv + notes_weekly.csv", _ai)

    status = {"refreshed_at": dt.datetime.now().astimezone().isoformat(timespec="minutes"),
              "ok": not errors, "refreshed": files, "failed": errors}
    _write(out("refresh_status.json"), json.dumps([status], indent=1))
    print(json.dumps(status, indent=1))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
