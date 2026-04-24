from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, text
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampedBase(SQLModel):
    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("now()"), "nullable": False},
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={
            "server_default": text("now()"),
            "nullable": False,
            "onupdate": utcnow,
        },
    )
