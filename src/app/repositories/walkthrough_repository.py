from __future__ import annotations

import json
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


def decode_screenshots(row: TicketWalkthrough | None) -> list[dict]:
    """Decode a walkthrough row's ``screenshots`` JSON blob into a list.

    Returns [] when the row is missing, the column is empty, or the stored
    value fails to parse into a list of ``{filename, url}`` objects — the
    walkthrough is optional metadata, so a bad blob should never break the
    ticket page.
    """
    if row is None or not row.screenshots:
        return []
    try:
        parsed = json.loads(row.screenshots)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    cleaned: list[dict] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        if not url:
            continue
        filename = (entry.get("filename") or "screenshot").strip() or "screenshot"
        cleaned.append({"filename": filename, "url": url})
    return cleaned


def derive_readiness(
    walkthrough: dict,
    uat_complexity: str | None,
) -> dict:
    """Compute the UAT-readiness signal from a serialized walkthrough.

    Single source of truth for "does this ticket have walkthrough material?" —
    both the walkthrough card and the Pass-to-UAT gate historically re-derived
    this rule in JS and drifted. Returning it from the API keeps them aligned
    and lets the Pass-to-UAT route (future) enforce the same rule server-side.

    ``sources`` names which fields carry material — useful for UI hints like
    "Notes and screenshots attached" without the caller re-inspecting the row.
    """
    sources: list[str] = []
    if walkthrough.get("loom_url"):
        sources.append("loom")
    shots = walkthrough.get("screenshots") or []
    if isinstance(shots, list) and len(shots) > 0:
        sources.append("screenshots")
    notes = walkthrough.get("notes")
    if isinstance(notes, str) and notes.strip():
        sources.append("notes")
    present = len(sources) > 0
    return {
        "walkthrough_present": present,
        "walkthrough_sources": sources,
        "needs_walkthrough": uat_complexity == "high" and not present,
    }


async def upsert_walkthrough(
    session: AsyncSession,
    *,
    ticket_key: str,
    loom_url: str | None,
    notes: str | None,
    screenshots: list[dict],
) -> TicketWalkthrough:
    """Create or update the single walkthrough row for ``ticket_key``.

    ``screenshots`` is the desired final list — pass ``[]`` to clear all
    attached screenshots. Each entry must have ``url`` (required) and
    ``filename`` (defaulted to ``"screenshot"`` when blank).
    """
    key = ticket_key.upper()
    row = await get_walkthrough(session, ticket_key=key)
    if row is None:
        row = TicketWalkthrough(ticket_key=key)
        session.add(row)
    row.loom_url = loom_url or None
    row.notes = notes or None
    normalized: list[dict] = []
    for entry in screenshots or []:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        if not url:
            continue
        filename = (entry.get("filename") or "").strip() or "screenshot"
        normalized.append({"filename": filename, "url": url})
    row.screenshots = json.dumps(normalized) if normalized else None
    row.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(row)
    return row
