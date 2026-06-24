from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.app.db.models.ticket_walkthrough import TicketWalkthrough


async def get_walkthrough(
    session: AsyncSession,
    *,
    ticket_key: str,
) -> TicketWalkthrough | None:
    """Return the walkthrough row for `ticket_key`, or None if none exists."""
    stmt = select(TicketWalkthrough).where(
        TicketWalkthrough.ticket_key == ticket_key.upper()
    )
    return (await session.exec(stmt)).first()


async def upsert_walkthrough(
    session: AsyncSession,
    *,
    ticket_key: str,
    loom_url: str | None,
    screenshot_url: str | None,
    notes: str | None,
) -> TicketWalkthrough:
    """Create or update the single walkthrough row for `ticket_key`.

    Values are replaced wholesale (the editor sends the full state each save),
    so passing None/"" for a field clears it.
    """
    key = ticket_key.upper()
    row = await get_walkthrough(session, ticket_key=key)
    if row is None:
        row = TicketWalkthrough(ticket_key=key)
        session.add(row)
    row.loom_url = loom_url or None
    row.screenshot_url = screenshot_url or None
    row.notes = notes or None
    row.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(row)
    return row
