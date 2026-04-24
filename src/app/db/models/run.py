from __future__ import annotations

from decimal import Decimal
from enum import Enum

from sqlalchemy import CheckConstraint, Column, Index, String
from sqlalchemy.dialects.postgresql import ARRAY, ENUM as PgEnum
from sqlmodel import Field

from src.app.db.base import TimestampedBase


class RunType(str, Enum):
    test_plan = "test_plan"
    test_plan_multi = "test_plan_multi"
    bug_lens = "bug_lens"
    bug_lens_multi = "bug_lens_multi"
    summarize = "summarize"


class RunStatus(str, Enum):
    ok = "ok"
    error = "error"


class Run(TimestampedBase, table=True):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("latency_ms >= 0", name="ck_runs_latency_nonneg"),
        CheckConstraint("prompt_tokens >= 0", name="ck_runs_prompt_tokens_nonneg"),
        CheckConstraint("output_tokens >= 0", name="ck_runs_output_tokens_nonneg"),
        CheckConstraint("cost_usd >= 0", name="ck_runs_cost_nonneg"),
        Index("ix_runs_created_at_desc", "created_at"),
        Index("ix_runs_user_created_at", "user_id", "created_at"),
    )

    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)

    run_type: RunType = Field(
        sa_column=Column(
            PgEnum(RunType, name="run_type", create_type=True),
            nullable=False,
            index=True,
        )
    )
    status: RunStatus = Field(
        sa_column=Column(
            PgEnum(RunStatus, name="run_status", create_type=True),
            nullable=False,
            index=True,
        )
    )

    ticket_keys: list[str] = Field(
        sa_column=Column(ARRAY(String(length=64)), nullable=False)
    )

    model: str = Field(nullable=False, max_length=128)
    llm_provider: str = Field(nullable=False, max_length=32)

    latency_ms: int = Field(nullable=False, default=0)
    prompt_tokens: int = Field(nullable=False, default=0)
    output_tokens: int = Field(nullable=False, default=0)
    cost_usd: Decimal = Field(
        nullable=False,
        default=Decimal("0"),
        max_digits=10,
        decimal_places=6,
    )

    error_code: str | None = Field(default=None, max_length=128)

    had_pr_diff: bool = Field(nullable=False, default=False)
    had_figma: bool = Field(nullable=False, default=False)
    had_parent: bool = Field(nullable=False, default=False)
    linked_ticket_count: int = Field(nullable=False, default=0)
    pr_count: int = Field(nullable=False, default=0)
    comment_count: int = Field(nullable=False, default=0)
