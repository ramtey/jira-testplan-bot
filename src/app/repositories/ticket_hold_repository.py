from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.app.db import crud
from src.app.db.models.ticket_hold import TicketHold


async def get_hold(
    db: AsyncIOMotorDatabase,
    *,
    ticket_key: str,
) -> TicketHold | None:
    """Return the active hold on `ticket_key`, or None if the ticket isn't held."""
    return await crud.find_one(db, TicketHold, {"ticket_key": ticket_key.upper()})


async def upsert_hold(
    db: AsyncIOMotorDatabase,
    *,
    ticket_key: str,
    reason: str,
    note: str | None = None,
) -> TicketHold:
    """Put `ticket_key` on hold, or update the reason/note of an existing hold.

    Editing an existing hold keeps its ``created_at`` so the "held since" stamp
    still reflects when testing actually stopped — a QA lead correcting the
    reason shouldn't reset the clock.
    """
    key = ticket_key.upper()
    row = await get_hold(db, ticket_key=key)
    if row is None:
        row = TicketHold(ticket_key=key, reason=reason)
    row.reason = reason
    row.note = (note or "").strip() or None
    if row.id is None:
        return await crud.insert(db, row)
    return await crud.save(db, row)


async def clear_hold(db: AsyncIOMotorDatabase, *, ticket_key: str) -> bool:
    """Resume `ticket_key`. Returns True if a hold was actually removed.

    The document is deleted rather than marked inactive: a resumed ticket carries
    no hold state, and the absence of a document is what every reader already
    treats as "not held".
    """
    result = await db[TicketHold.__collection__].delete_one(
        {"ticket_key": ticket_key.upper()}
    )
    return result.deleted_count > 0
