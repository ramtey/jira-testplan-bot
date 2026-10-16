"""The last auto-post outcome per ticket, so the plan view can say why a plan
was not posted when the skip happened in the watcher, out of sight.

Its own collection, keyed by ticket, rather than a field on `jira_tickets`:
`crud.save` there `$set`s the whole loaded document on every ticket fetch, so
a fetch overlapping a sweep would write the stale value back over this one.
"""

from __future__ import annotations

from datetime import timezone

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from src.app.db.base import utcnow

COLLECTION = "auto_post_outcomes"
# Append-only history for `testplan auto-post`: one row per change of a
# ticket's outcome, plus manual posts. Not one per sweep — a ticket the sweep
# re-checks every five minutes would bury the week in identical skips.
EVENTS = "auto_post_events"


async def record(
    db: AsyncIOMotorDatabase, *, ticket_key: str, outcome: dict
) -> dict | None:
    """Store `outcome` as the ticket's latest; return the one it replaced.

    One atomic read-and-write, so two posts racing cannot both see the same
    "previous" — which is what the alerting uses to say a problem once.
    """
    return await db[COLLECTION].find_one_and_update(
        {"_id": ticket_key.upper()},
        {"$set": {**outcome, "at": utcnow()}},
        upsert=True,
        return_document=ReturnDocument.BEFORE,
    )


async def add_event(db: AsyncIOMotorDatabase, *, ticket_key: str, event: dict) -> None:
    await db[EVENTS].insert_one({**event, "ticket_key": ticket_key.upper(), "at": utcnow()})


async def list_events_since(db: AsyncIOMotorDatabase, *, since) -> list[dict]:
    """Events recorded at or after `since`, newest first."""
    cursor = db[EVENTS].find({"at": {"$gte": since}}, {"_id": 0}).sort("at", -1)
    out = []
    for doc in await cursor.to_list(length=None):
        at = doc.get("at")
        if at is not None and at.tzinfo is None:
            doc["at"] = at.replace(tzinfo=timezone.utc)
        out.append(doc)
    return out


async def list_since(db: AsyncIOMotorDatabase, *, since) -> list[dict]:
    """Every ticket's latest outcome recorded at or after `since`, newest first."""
    cursor = db[COLLECTION].find({"at": {"$gte": since}}).sort("at", -1)
    out = []
    for doc in await cursor.to_list(length=None):
        at = doc.get("at")
        if at is not None and at.tzinfo is None:
            doc["at"] = at.replace(tzinfo=timezone.utc)
        doc["ticket_key"] = doc.pop("_id")
        out.append(doc)
    return out


async def get_last(db: AsyncIOMotorDatabase, *, ticket_key: str) -> dict | None:
    doc = await db[COLLECTION].find_one({"_id": ticket_key.upper()})
    if doc is None:
        return None
    at = doc.get("at")
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return {
        "action": doc.get("action"),
        "reason": doc.get("reason"),
        "detail": doc.get("detail"),
        "plan_id": doc.get("plan_id"),
        "at": at.isoformat() if at else None,
    }
