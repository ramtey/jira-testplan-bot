"""The last auto-post outcome per ticket, so the plan view can say why a plan
was not posted when the skip happened in the watcher, out of sight.

Its own collection, keyed by ticket, rather than a field on `jira_tickets`:
`crud.save` there `$set`s the whole loaded document on every ticket fetch, so
a fetch overlapping a sweep would write the stale value back over this one.
"""

from __future__ import annotations

from datetime import timezone

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.app.db.base import utcnow

COLLECTION = "auto_post_outcomes"


async def record(db: AsyncIOMotorDatabase, *, ticket_key: str, outcome: dict) -> None:
    await db[COLLECTION].update_one(
        {"_id": ticket_key.upper()},
        {"$set": {**outcome, "at": utcnow()}},
        upsert=True,
    )


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
