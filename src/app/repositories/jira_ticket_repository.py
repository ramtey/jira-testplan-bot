from __future__ import annotations

from datetime import datetime

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.app.db.base import utcnow
from src.app.db.models.jira_ticket import JiraTicket


def _project_key_from_ticket(ticket_key: str) -> str:
    return ticket_key.split("-", 1)[0] if "-" in ticket_key else ticket_key


async def get_auto_bug_analysis_dispatched_at(
    session: AsyncSession, *, ticket_key: str
) -> datetime | None:
    result = await session.exec(
        select(JiraTicket.auto_bug_analysis_dispatched_at).where(
            JiraTicket.ticket_key == ticket_key
        )
    )
    return result.first()


async def mark_auto_bug_analysis_dispatched(
    session: AsyncSession, *, ticket_key: str
) -> datetime:
    """Stamp the auto-dispatch timestamp on the ticket if not already set.

    Upserts the row (so the first-ever fetch for a brand-new ticket can still
    claim it) and returns the effective dispatched_at — the existing value if
    one was already stored, or the freshly written `now()`.
    """
    result = await session.exec(select(JiraTicket).where(JiraTicket.ticket_key == ticket_key))
    ticket = result.first()
    now = utcnow()

    if ticket is None:
        ticket = JiraTicket(
            ticket_key=ticket_key,
            project_key=_project_key_from_ticket(ticket_key),
            auto_bug_analysis_dispatched_at=now,
        )
        session.add(ticket)
        await session.flush()
        return now

    if ticket.auto_bug_analysis_dispatched_at is None:
        ticket.auto_bug_analysis_dispatched_at = now
        session.add(ticket)
        await session.flush()
        return now

    return ticket.auto_bug_analysis_dispatched_at


async def upsert_snapshot(
    session: AsyncSession,
    *,
    ticket_key: str,
    issue_type: str | None = None,
    status: str | None = None,
    title: str | None = None,
    parent_key: str | None = None,
) -> JiraTicket:
    result = await session.exec(select(JiraTicket).where(JiraTicket.ticket_key == ticket_key))
    ticket = result.first()
    project_key = _project_key_from_ticket(ticket_key)

    if ticket is None:
        ticket = JiraTicket(
            ticket_key=ticket_key,
            project_key=project_key,
            issue_type=issue_type,
            status=status,
            title=title,
            parent_key=parent_key,
        )
        session.add(ticket)
        await session.flush()
        return ticket

    if issue_type:
        ticket.issue_type = issue_type
    if status:
        ticket.status = status
    if title:
        ticket.title = title
    if parent_key:
        ticket.parent_key = parent_key
    ticket.last_seen_at = utcnow()
    session.add(ticket)
    await session.flush()
    return ticket
