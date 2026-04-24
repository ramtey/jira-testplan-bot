from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, UniqueConstraint
from sqlmodel import Field

from src.app.db.base import TimestampedBase, utcnow


class User(TimestampedBase, table=True):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    email: str = Field(nullable=False, max_length=320, index=True)
    jira_account_id: str | None = Field(default=None, max_length=128, index=True)
    display_name: str | None = Field(default=None, max_length=255)
    first_seen_at: datetime = Field(
        default_factory=utcnow,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"nullable": False},
    )
    last_seen_at: datetime = Field(
        default_factory=utcnow,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"nullable": False},
    )
