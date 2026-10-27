"""How long tickets wait in the QA queue before the bot has a plan for them.

For each ticket that entered the ready-for-QA status since ``--since``: the first
time it did (Jira changelog), the first successful test-plan run covering it
(Mongo), and the wait between the two. A plan made before the ticket reached the
queue counts as zero wait. Tickets the bot never planned are kept, with no wait,
so coverage can be reported beside the median.

Usage:
    uv run python scripts/plan_wait_metrics.py --since 2025-12-01 \
        --split 2026-09-08 --out plan_wait_tickets.csv

``--split`` divides the rows into two eras (e.g. before/after the queue watcher
went live) and prints a median for each. Read-only against Jira and Mongo.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import statistics
import sys

import certifi
import httpx
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app.config import settings  # noqa: E402
from src.app.db.mongo import _resolve_db_name, _resolve_mongo_uri, _uses_tls  # noqa: E402

PLAN_RUN_TYPES = ["test_plan", "test_plan_multi"]


def _parse_jira_ts(ts: str) -> dt.datetime:
    # Jira: 2026-10-23T14:02:39.315-0700 (no colon in the offset)
    return dt.datetime.strptime(ts[:23] + ts[23:].replace(":", ""), "%Y-%m-%dT%H:%M:%S.%f%z")


def _jql_keys(client: httpx.Client, jql: str) -> list[str]:
    keys: set[str] = set()
    token = None
    while True:
        body = {"jql": jql, "fields": ["key"], "maxResults": 100}
        if token:
            body["nextPageToken"] = token
        r = client.post("/rest/api/3/search/jql", json=body)
        r.raise_for_status()
        data = r.json()
        keys.update(i["key"] for i in data.get("issues", []))
        token = data.get("nextPageToken")
        if data.get("isLast", True) or not token:
            return sorted(keys)


def _first_entry(client: httpx.Client, key: str, status: str, since: dt.datetime) -> dt.datetime | None:
    """Earliest move into ``status`` at or after ``since``, from the full changelog."""
    first, start = None, 0
    while True:
        r = client.get(f"/rest/api/3/issue/{key}/changelog", params={"startAt": start, "maxResults": 100})
        r.raise_for_status()
        data = r.json()
        for history in data.get("values", []):
            for item in history.get("items", []):
                if item.get("field") != "status":
                    continue
                if (item.get("toString") or "").strip().lower() != status.lower():
                    continue
                at = _parse_jira_ts(history["created"])
                if at >= since and (first is None or at < first):
                    first = at
        start += data.get("maxResults", 100)
        if data.get("isLast", True) or start >= data.get("total", 0):
            return first


def _first_plans(keys: set[str]) -> dict[str, dt.datetime]:
    uri = _resolve_mongo_uri()
    tls = {"tlsCAFile": certifi.where()} if _uses_tls(uri) else {}
    db = MongoClient(uri, serverSelectionTimeoutMS=20_000, tz_aware=True, **tls)[_resolve_db_name(uri)]
    runs = {
        r["_id"]: r["ticket_keys"]
        for r in db["runs"].find(
            {"status": "ok", "run_type": {"$in": PLAN_RUN_TYPES}, "ticket_keys": {"$in": sorted(keys)}},
            {"ticket_keys": 1},
        )
    }
    first: dict[str, dt.datetime] = {}
    for plan in db["generated_plans"].find({"run_id": {"$in": list(runs)}}, {"run_id": 1, "created_at": 1}):
        for key in runs[plan["run_id"]]:
            if key in keys and (key not in first or plan["created_at"] < first[key]):
                first[key] = plan["created_at"]
    return first


def _summary(rows: list[dict]) -> dict:
    waits = [r["wait_hours"] for r in rows if r["wait_hours"] != ""]
    return {
        "tickets": len(rows),
        "with_bot_plan": len(waits),
        "median_wait_hours": round(statistics.median(waits), 2) if waits else None,
        "within_1h": sum(1 for w in waits if w <= 1),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--project", default=(settings.workflow_project_prefixes or [None])[0],
                   help="Jira project key (default: first of WORKFLOW_PROJECT_PREFIXES)")
    p.add_argument("--ready-status", default="Ready To Test", help="status that puts a ticket in the QA queue")
    p.add_argument("--since", required=True, help="YYYY-MM-DD; first queue entry on or after this date")
    p.add_argument("--split", help="YYYY-MM-DD; rows before are era 'before', on or after are 'after'")
    p.add_argument("--out", help="write one CSV row per ticket here")
    args = p.parse_args()
    if not args.project:
        sys.exit("No project: pass --project or set WORKFLOW_PROJECT_PREFIXES in .env")

    # astimezone() with no argument uses the machine's zone with its DST rules, so a
    # December date gets its winter offset, not today's.
    since = dt.datetime.fromisoformat(args.since).astimezone()
    split = dt.date.fromisoformat(args.split) if args.split else None

    with httpx.Client(base_url=settings.jira_url, auth=(settings.jira_username, settings.jira_api_token),
                      timeout=60) as jira:
        keys = _jql_keys(jira, f'project = {args.project} AND status CHANGED TO "{args.ready_status}" '
                               f'AFTER "{args.since}"')
        print(f"{len(keys)} tickets entered {args.ready_status!r} since {args.since}", file=sys.stderr)
        ready = {k: _first_entry(jira, k, args.ready_status, since) for k in keys}

    ready = {k: v for k, v in ready.items() if v is not None}
    plans = _first_plans(set(ready))

    rows = []
    for key, entered in sorted(ready.items(), key=lambda kv: kv[1]):
        plan = plans.get(key)
        wait = "" if plan is None else round(max(0.0, (plan - entered).total_seconds() / 3600), 2)
        day = entered.astimezone().date()
        rows.append({
            "ticket": key,
            "ready_at": entered.astimezone().isoformat(timespec="minutes"),
            "plan_at": plan.astimezone().isoformat(timespec="minutes") if plan else "",
            "wait_hours": wait,
            "outcome": "no_bot_plan" if plan is None else ("ready_before_queue" if plan <= entered else "after_queue"),
            "week_start": str(day - dt.timedelta(days=day.weekday())),
            "era": "" if split is None else ("after" if day >= split else "before"),
        })

    if args.out and rows:
        with open(args.out, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {len(rows)} rows to {args.out}", file=sys.stderr)

    eras = {"all": rows} if split is None else {
        e: [r for r in rows if r["era"] == e] for e in ("before", "after")}
    for name, group in eras.items():
        print(name, _summary(group))


if __name__ == "__main__":
    main()
