from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field

from src.app.db.base import DocumentBase, utcnow


class User(DocumentBase):
    __collection__: ClassVar[str] = "users"

    email: str
    jira_account_id: str | None = None
    display_name: str | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
