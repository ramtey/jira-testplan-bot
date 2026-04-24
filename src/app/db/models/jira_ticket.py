from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, UniqueConstraint
from sqlmodel import Field

from src.app.db.base import TimestampedBase, utcnow


class JiraTicket(TimestampedBase, table=True):
    __tablename__ = "jira_tickets"
    __table_args__ = (UniqueConstraint("ticket_key", name="uq_jira_tickets_key"),)

    ticket_key: str = Field(nullable=False, max_length=64, index=True)
    project_key: str = Field(nullable=False, max_length=32, index=True)
    issue_type: str | None = Field(default=None, max_length=32)
    status: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=512)
    parent_key: str | None = Field(default=None, max_length=64, index=True)
    last_seen_at: datetime = Field(
        default_factory=utcnow,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"nullable": False},
    )
