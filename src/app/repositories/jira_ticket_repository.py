from __future__ import annotations

from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.app.db import crud
from src.app.db.base import utcnow
from src.app.db.models.jira_ticket import JiraTicket


def _project_key_from_ticket(ticket_key: str) -> str:
    return ticket_key.split("-", 1)[0] if "-" in ticket_key else ticket_key


async def get_auto_bug_analysis_dispatched_at(
    db: AsyncIOMotorDatabase, *, ticket_key: str
) -> datetime | None:
    doc = await db[JiraTicket.__collection__].find_one(
        {"ticket_key": ticket_key},
        {"auto_bug_analysis_dispatched_at": 1},
    )
    if doc is None:
        return None
    value = doc.get("auto_bug_analysis_dispatched_at")
    if value is not None and value.tzinfo is None:
        from datetime import timezone

        return value.replace(tzinfo=timezone.utc)
    return value


async def mark_auto_bug_analysis_dispatched(
    db: AsyncIOMotorDatabase, *, ticket_key: str
) -> datetime:
    """Stamp the auto-dispatch timestamp on the ticket if not already set.

    Upserts the document (so the first-ever fetch for a brand-new ticket can still
    claim it) and returns the effective dispatched_at — the existing value if
    one was already stored, or the freshly written `now()`.
    """
    ticket = await crud.find_one(db, JiraTicket, {"ticket_key": ticket_key})
    now = utcnow()

    if ticket is None:
        ticket = JiraTicket(
            ticket_key=ticket_key,
            project_key=_project_key_from_ticket(ticket_key),
            auto_bug_analysis_dispatched_at=now,
        )
        await crud.insert(db, ticket)
        return now

    if ticket.auto_bug_analysis_dispatched_at is None:
        ticket.auto_bug_analysis_dispatched_at = now
        await crud.save(db, ticket)
        return now

    return ticket.auto_bug_analysis_dispatched_at


async def upsert_snapshot(
    db: AsyncIOMotorDatabase,
    *,
    ticket_key: str,
    issue_type: str | None = None,
    status: str | None = None,
    title: str | None = None,
    parent_key: str | None = None,
) -> JiraTicket:
    ticket = await crud.find_one(db, JiraTicket, {"ticket_key": ticket_key})
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
        return await crud.insert(db, ticket)

    if issue_type:
        ticket.issue_type = issue_type
    if status:
        ticket.status = status
    if title:
        ticket.title = title
    if parent_key:
        ticket.parent_key = parent_key
    ticket.last_seen_at = utcnow()
    return await crud.save(db, ticket)
