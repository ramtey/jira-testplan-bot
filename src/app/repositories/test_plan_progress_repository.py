from __future__ import annotations

import json
import re

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.app.db import crud
from src.app.db.models.test_plan_progress import TestPlanProgress


async def get_progress(
    db: AsyncIOMotorDatabase,
    *,
    progress_key: str,
) -> TestPlanProgress | None:
    """Return the progress document for `progress_key`, or None if none exists."""
    return await crud.find_one(
        db, TestPlanProgress, {"progress_key": progress_key.upper()}
    )


async def upsert_progress(
    db: AsyncIOMotorDatabase,
    *,
    progress_key: str,
    checked_ids: list[str],
) -> TestPlanProgress:
    """Create or replace the single progress document for `progress_key`.

    The client sends the full set of checked ids each save, so the stored value
    is replaced wholesale.
    """
    key = progress_key.upper()
    # Normalize: dedupe, drop non-strings, keep a stable order so saves are idempotent.
    cleaned = sorted({c for c in checked_ids if isinstance(c, str)})
    row = await get_progress(db, progress_key=key)
    if row is None:
        row = TestPlanProgress(progress_key=key, checked_ids=json.dumps(cleaned))
        return await crud.insert(db, row)
    row.checked_ids = json.dumps(cleaned)
    return await crud.save(db, row)


async def find_for_same_tickets(
    db: AsyncIOMotorDatabase,
    *,
    progress_key: str,
) -> list[TestPlanProgress]:
    """Every progress document for the ticket(s) in `progress_key`, any shape.

    The key is ``<TICKETS>:<fingerprint>``, and the fingerprint is the plan's
    section sizes — so regenerating a plan into a different shape moves progress
    to a new key and leaves the old one behind. Nothing could see those orphans,
    which is why a ticket with real QA marks renders as 0%: the UI polls the
    current shape's key, gets a 404, and shows an empty checklist that reads as
    "nothing was tested".

    Returned newest-first, including the key asked about if it exists; the
    caller decides what to do with its own shape.
    """
    prefix = progress_key.rsplit(":", 1)[0].upper()
    return await crud.find_many(
        db,
        TestPlanProgress,
        {"progress_key": {"$regex": f"^{re.escape(prefix)}:"}},
        sort=[("updated_at", -1)],
    )
