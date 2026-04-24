from __future__ import annotations

from sqlmodel.ext.asyncio.session import AsyncSession

from src.app.db.models.feedback import FeedbackEvent, FeedbackSignal, FeedbackTarget


async def record(
    session: AsyncSession,
    *,
    user_id: int,
    target_type: FeedbackTarget,
    target_id: int,
    signal: FeedbackSignal,
    note: str | None = None,
) -> FeedbackEvent:
    event = FeedbackEvent(
        user_id=user_id,
        target_type=target_type,
        target_id=target_id,
        signal=signal,
        note=note,
    )
    session.add(event)
    await session.flush()
    return event
