"""UAT readiness service — composes the walkthrough row and the latest plan's
UAT complexity into a single decision the API responses and the Pass-to-UAT
gate both consume. Kept out of the repositories package so it can depend on
`walkthrough_repository` and `plan_repository` without creating a circular
import through `repositories/__init__.py`.
"""

from __future__ import annotations

import json

from sqlmodel.ext.asyncio.session import AsyncSession

from src.app.repositories import plan_repository, walkthrough_repository


async def fetch_readiness(
    session: AsyncSession,
    *,
    ticket_key: str,
) -> dict:
    """Return the enriched walkthrough shape for a ticket.

    Shape:
        {
          "loom_url", "screenshots", "notes", "updated_at",
          "uat_complexity",
          "walkthrough_present", "walkthrough_sources", "needs_walkthrough",
        }

    Used by GET/PUT `/tickets/{key}/walkthrough` and by the Pass-to-UAT gate
    on the workflow route — computing it in one place stops the response
    surface and the enforcement rule from drifting apart.
    """
    row = await walkthrough_repository.get_walkthrough(
        session, ticket_key=ticket_key
    )
    data = _serialize_walkthrough(row)
    complexity = await _fetch_complexity(session, ticket_key)
    data["uat_complexity"] = complexity
    data.update(walkthrough_repository.derive_readiness(data, complexity))
    return data


def _serialize_walkthrough(row) -> dict:
    if row is None:
        return {
            "loom_url": None,
            "screenshots": [],
            "notes": None,
            "updated_at": None,
        }
    return {
        "loom_url": row.loom_url,
        "screenshots": walkthrough_repository.decode_screenshots(row),
        "notes": row.notes,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _fetch_complexity(session: AsyncSession, ticket_key: str) -> str | None:
    latest = await plan_repository.find_latest_plan_for_ticket(
        session, ticket_key=ticket_key.upper()
    )
    if not latest or not latest.body:
        return None
    try:
        return (json.loads(latest.body) or {}).get("uat_complexity")
    except (ValueError, TypeError):
        return None
