"""The dispatcher's side of unattended runs: claim, lease, recover, heartbeat.

`testplan uat-dispatch --once` runs on a schedule on the machine that has the
devices. Each tick it asks ``claim`` for one run to work on. Claiming:

* recovers runs whose lease expired (the dispatcher died mid-run): their open
  legs are closed as ``run_aborted``, which gets the one retry round;
* takes no run while another holds an unexpired lease, because the device
  (one simulator) is shared;
* takes no run that cannot finish before the working window closes;
* cancels a run whose ticket moved out of the status it was queued in, or
  whose plan is no longer the live one;
* routes the next round (builds read now) and leases the run for as long as
  its legs may take.

Everything a tick decided is returned, so the dispatcher can alert on it, and
recorded in the heartbeat, so ``/health/uat`` can say when it stopped.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from .. import watch_schedule
from ..config import settings
from ..db.base import utcnow
from ..db.models.uat_run import UatRun
from ..jira_client import JiraClient
from ..repositories import uat_run_repository
from . import uat_runs
from .uat_runs import RunRefused

logger = logging.getLogger(__name__)

HEARTBEAT_COLLECTION = "watcher_heartbeats"
HEARTBEAT_ID = "uat_dispatch"
DISPATCH_INTERVAL_SECONDS = 300
# A run the tracker could not be read for is tried again after this long.
RETRY_AFTER_MIN = 10


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def reap_expired(db: AsyncIOMotorDatabase, *, now: datetime | None = None) -> list[int]:
    """Close the open legs of runs whose dispatcher lease ran out."""
    now = now or utcnow()
    reaped = []
    async for doc in db[UatRun.__collection__].find(
            {"status": "running", "lease.expires_at": {"$lt": now}}):
        run = await uat_run_repository.get(db, doc["_id"])
        for name, leg in run.legs.items():
            if leg.get("status") in ("pending", "running"):
                run = await uat_runs.finalize_leg(db, run, name, {
                    "exit_code": -1,
                    "detail": "the dispatcher's lease expired mid-run (it stopped or the machine slept)"})
        run = await uat_run_repository.get(db, run.id)
        if run.status == "running":
            # Legs were already closed but the run was not: close it now.
            await uat_runs._close(db, run)
        reaped.append(run.id)
    return reaped


async def _take(db, run_id: int, status: str, holder: dict, now: datetime) -> bool:
    doc = await db[UatRun.__collection__].find_one_and_update(
        {"_id": run_id, "status": status,
         "$or": [{"lease": None}, {"lease.expires_at": {"$lt": now}}]},
        {"$set": {"lease": {**holder, "expires_at": now + timedelta(minutes=15)},
                  "updated_at": now}},
        return_document=ReturnDocument.AFTER)
    return doc is not None


async def _release(db, run: UatRun, *, retry_after_min: int | None = None, reason: str | None = None):
    run.lease = None
    if retry_after_min is not None:
        run.not_before = utcnow() + timedelta(minutes=retry_after_min)
    if reason:
        run.status_reason = reason
    await uat_run_repository.save(db, run)


async def claim(db: AsyncIOMotorDatabase, *, holder: dict, jira: JiraClient | None = None,
                profiles: dict | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Pick one due run, route its next round, and lease it.

    Returns {"claimed": run_id | None, "reason", "events": [...]} where each
    event is {run_id, ticket_key, event, detail} for something this tick
    decided (cancelled, waiting, review opened, error).
    """
    now = now or utcnow()
    events: list[dict] = []

    def event(run: UatRun, kind: str, detail: str | None = None):
        events.append({"run_id": run.id, "ticket_key": run.ticket_key, "event": kind,
                       "detail": detail})

    for run_id in await reap_expired(db, now=now):
        run = await uat_run_repository.get(db, run_id)
        event(run, "lease_expired", run.status_reason)

    busy = await db[UatRun.__collection__].find_one(
        {"status": "running", "lease.expires_at": {"$gte": now}})
    if busy is not None:
        return {"claimed": None, "reason": f"run {busy['_id']} ({busy['ticket_key']}) holds the device",
                "events": events}
    left = watch_schedule.minutes_left()
    if left is not None and left < settings.uat_leg_wall_clock_min:
        return {"claimed": None, "reason": f"the working window closes in {int(left)} min, "
                f"less than one leg's {settings.uat_leg_wall_clock_min} min cap", "events": events}

    # Read after reaping: a recovered run is due at once, in this tick.
    due = await db[UatRun.__collection__].find(
        {"status": {"$in": ["queued", "waiting_for_build"]},
         "$or": [{"not_before": None}, {"not_before": {"$lte": utcnow()}}]}
    ).sort([("not_before", 1), ("_id", 1)]).to_list(50)
    jira = jira or JiraClient()
    for doc in due:
        if not await _take(db, doc["_id"], doc["status"], holder, now):
            continue
        run = await uat_run_repository.get(db, doc["_id"])
        was_waiting = run.status == "waiting_for_build"
        try:
            status = await jira.get_issue_status(run.ticket_key)
        except Exception as e:
            detail = f"tracker unreadable ({type(e).__name__}: {e}); retrying in {RETRY_AFTER_MIN} min"
            await _release(db, run, retry_after_min=RETRY_AFTER_MIN)
            event(run, "error", detail)
            continue
        started = (run.ticket_snapshot or {}).get("status")
        if started and status and status != started:
            await uat_runs.cancel(db, run, reason=f"ticket moved to {status}")
            event(run, "cancelled", f"ticket moved to {status}")
            continue
        live = await uat_runs._live_own_plan(db, run.ticket_key)
        if live is None or live[1].id != run.plan_id:
            what = f"plan v{live[1].version} is live now" if live else "no plan is live"
            await uat_runs.cancel(db, run, reason=f"stale_plan: {what}", code="plan_changed")
            event(run, "cancelled", f"stale plan: {what}")
            continue
        try:
            run = await uat_runs.route_round(db, run, jira=jira, profiles=profiles)
        except RunRefused as e:
            await _release(db, run, retry_after_min=RETRY_AFTER_MIN, reason=f"{e.code}: {e.message}")
            event(run, "error", f"{e.code}: {e.message}")
            continue
        if run.status == "running":
            pending = sum(1 for l in run.legs.values() if l.get("status") == "pending")
            minutes = settings.uat_leg_wall_clock_min * max(1, pending) + 15
            run.lease = {**holder, "expires_at": now + timedelta(minutes=minutes)}
            await uat_run_repository.save(db, run)
            return {"claimed": run.id, "reason": None, "events": events}
        # Nothing ran this round: the run moved on by itself.
        if run.status == "waiting_for_build":
            event(run, "waiting_for_build" if not was_waiting else "still_waiting", run.status_reason)
        else:
            event(run, run.status, run.status_reason)
    return {"claimed": None, "reason": "nothing due" if not due else "no due run could be taken",
            "events": events}


async def release(db: AsyncIOMotorDatabase, run: UatRun) -> None:
    """The dispatcher is done with `run` (it closed, or it gave up)."""
    run = await uat_run_repository.get(db, run.id)
    if run is not None and run.lease is not None:
        await db[UatRun.__collection__].update_one({"_id": run.id}, {"$set": {"lease": None}})


async def alert_once(db: AsyncIOMotorDatabase, run_id: int, kind: str) -> bool:
    """Record that `kind` was alerted for the run; False if it already was."""
    res = await db[UatRun.__collection__].update_one(
        {"_id": run_id, f"alerts.{kind}": {"$exists": False}},
        {"$set": {f"alerts.{kind}": utcnow().isoformat()}})
    return res.modified_count == 1


async def cost_today(db: AsyncIOMotorDatabase) -> float:
    """Dollars spent by runs created since local midnight."""
    midnight = watch_schedule.current_time().replace(hour=0, minute=0, second=0, microsecond=0)
    total = 0.0
    async for doc in db[UatRun.__collection__].find(
            {"created_at": {"$gte": midnight.astimezone(timezone.utc)}}, {"cost_usd": 1, "legs": 1}):
        legs = doc.get("legs") or {}
        total += sum(float(l.get("cost_usd") or 0) for l in legs.values()) or float(doc.get("cost_usd") or 0)
    return round(total, 4)


async def record_heartbeat(db: AsyncIOMotorDatabase, *, outcome: str, run_id: int | None = None,
                           error: str | None = None) -> None:
    await db[HEARTBEAT_COLLECTION].update_one(
        {"_id": HEARTBEAT_ID},
        {"$set": {"at": utcnow(), "interval_seconds": DISPATCH_INTERVAL_SECONDS,
                  "outcome": outcome, "run_id": run_id, "error": error}},
        upsert=True)


async def health(db: AsyncIOMotorDatabase) -> dict[str, Any]:
    """Dispatcher liveness plus what is waiting on a person.

    ``state``: never (no dispatcher ever ran, so nothing is said), paused
    (outside the working window), ok, error (the last tick failed), late (no
    tick for three intervals inside the window: unattended runs stopped).
    """
    beat = await db[HEARTBEAT_COLLECTION].find_one({"_id": HEARTBEAT_ID})
    runs = db[UatRun.__collection__]
    review = await runs.find({"status": "awaiting_review"}, {"ticket_key": 1}).sort("_id", -1).to_list(50)
    out: dict[str, Any] = {
        "awaiting_review": [{"run_id": d["_id"], "ticket_key": d["ticket_key"]} for d in review],
        "queued": await runs.count_documents({"status": "queued"}),
        "waiting_for_build": await runs.count_documents({"status": "waiting_for_build"}),
        "running": [{"run_id": d["_id"], "ticket_key": d["ticket_key"]}
                    for d in await runs.find({"status": "running"}, {"ticket_key": 1}).to_list(10)],
    }
    if beat is None or beat.get("at") is None:
        return {**out, "state": "never", "last_tick_at": None}
    at = _aware(beat["at"])
    now = watch_schedule.current_time()
    minutes = int((now - at).total_seconds() // 60)
    opened = watch_schedule.window_start(now)
    since = max(at, opened) if opened else at
    late_after = 3 * int(beat.get("interval_seconds") or DISPATCH_INTERVAL_SECONDS)
    if not watch_schedule.is_active(now):
        state = "paused"
    elif (now - since).total_seconds() > late_after:
        # Measured from when the window opened at the latest, so a morning
        # before the first tick is not "late" for last night's silence.
        state = "late"
    elif beat.get("error"):
        state = "error"
    else:
        state = "ok"
    return {**out, "state": state, "last_tick_at": at.isoformat(), "minutes_since": minutes,
            "outcome": beat.get("outcome"), "error": beat.get("error")}
