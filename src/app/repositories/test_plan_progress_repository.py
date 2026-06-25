from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.app.db.models.test_plan_progress import TestPlanProgress


async def get_progress(
    session: AsyncSession,
    *,
    progress_key: str,
) -> TestPlanProgress | None:
    """Return the progress row for `progress_key`, or None if none exists."""
    stmt = select(TestPlanProgress).where(
        TestPlanProgress.progress_key == progress_key.upper()
    )
    return (await session.exec(stmt)).first()


async def upsert_progress(
    session: AsyncSession,
    *,
    progress_key: str,
    checked_ids: list[str],
) -> TestPlanProgress:
    """Create or replace the single progress row for `progress_key`.

    The client sends the full set of checked ids each save, so the stored value
    is replaced wholesale.
    """
    key = progress_key.upper()
    # Normalize: dedupe, drop non-strings, keep a stable order so saves are idempotent.
    cleaned = sorted({c for c in checked_ids if isinstance(c, str)})
    row = await get_progress(session, progress_key=key)
    if row is None:
        row = TestPlanProgress(progress_key=key)
        session.add(row)
    row.checked_ids = json.dumps(cleaned)
    row.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(row)
    return row
