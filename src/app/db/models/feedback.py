from __future__ import annotations

from enum import Enum

from sqlalchemy import Column, Index
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlmodel import Field

from src.app.db.base import TimestampedBase


class FeedbackSignal(str, Enum):
    up = "up"
    down = "down"


class FeedbackTarget(str, Enum):
    plan = "plan"
    case = "case"


class FeedbackEvent(TimestampedBase, table=True):
    __tablename__ = "feedback_events"
    __table_args__ = (
        Index("ix_feedback_events_target", "target_type", "target_id"),
        Index("ix_feedback_events_created_at", "created_at"),
    )

    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)
    target_type: FeedbackTarget = Field(
        sa_column=Column(
            PgEnum(FeedbackTarget, name="feedback_target", create_type=True),
            nullable=False,
        )
    )
    target_id: int = Field(nullable=False)
    signal: FeedbackSignal = Field(
        sa_column=Column(
            PgEnum(FeedbackSignal, name="feedback_signal", create_type=True),
            nullable=False,
        )
    )
    note: str | None = Field(default=None)
