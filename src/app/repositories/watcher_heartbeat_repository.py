"""When the queue watcher last finished a sweep, and how it went.

The watcher is a launchd job outside the API process. When it dies nothing
fails: plans just stop appearing and auto-posts stop landing, and the first
anyone hears of it is a tester wondering why. One document, rewritten after
every real sweep, is what lets the app say "the watcher has stopped".
"""

from __future__ import annotations

from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.app.db.base import utcnow

COLLECTION = "watcher_heartbeats"
_ID = "sweep"


async def record(
    db: AsyncIOMotorDatabase,
    *,
    interval_seconds: int,
    queue_error: str | None,
    generated: int,
    posted: int,
    failed: int,
) -> None:
    await db[COLLECTION].update_one(
        {"_id": _ID},
        {
            "$set": {
                "at": utcnow(),
                "interval_seconds": int(interval_seconds),
                "queue_error": queue_error,
                "generated": generated,
                "posted": posted,
                "failed": failed,
            }
        },
        upsert=True,
    )


async def get(db: AsyncIOMotorDatabase) -> dict | None:
    doc = await db[COLLECTION].find_one({"_id": _ID})
    if doc is None:
        return None
    at: datetime | None = doc.get("at")
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return {**doc, "at": at}
