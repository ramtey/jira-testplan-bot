from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.app.db.models.ticket_hold import TicketHold


async def get_hold(
    session: AsyncSession,
    *,
    ticket_key: str,
) -> TicketHold | None:
    """Return the active hold on `ticket_key`, or None if the ticket isn't held."""
    stmt = select(TicketHold).where(TicketHold.ticket_key == ticket_key.upper())
    return (await session.exec(stmt)).first()


async def upsert_hold(
    session: AsyncSession,
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
    row = await get_hold(session, ticket_key=key)
    if row is None:
        row = TicketHold(ticket_key=key)
        session.add(row)
    row.reason = reason
    row.note = (note or "").strip() or None
    row.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(row)
    return row


async def clear_hold(session: AsyncSession, *, ticket_key: str) -> bool:
    """Resume `ticket_key`. Returns True if a hold was actually removed.

    The row is deleted rather than marked inactive: a resumed ticket carries no
    hold state, and the absence of a row is what every reader already treats as
    "not held".
    """
    row = await get_hold(session, ticket_key=ticket_key)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True
