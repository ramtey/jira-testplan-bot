from __future__ import annotations

from enum import Enum
from typing import ClassVar

from src.app.db.base import DocumentBase


class FeedbackSignal(str, Enum):
    up = "up"
    down = "down"


class FeedbackTarget(str, Enum):
    plan = "plan"
    case = "case"


class FeedbackEvent(DocumentBase):
    __collection__: ClassVar[str] = "feedback_events"

    user_id: int
    target_type: FeedbackTarget
    target_id: int
    signal: FeedbackSignal
    note: str | None = None
