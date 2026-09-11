from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.app.db import crud
from src.app.db.models.feedback import FeedbackEvent, FeedbackSignal, FeedbackTarget


async def record(
    db: AsyncIOMotorDatabase,
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
    return await crud.insert(db, event)
