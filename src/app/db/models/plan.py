from __future__ import annotations

from enum import Enum

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlmodel import Field

from src.app.db.base import TimestampedBase


class PlanFormat(str, Enum):
    markdown = "markdown"
    jira = "jira"
    json = "json"


class GeneratedPlan(TimestampedBase, table=True):
    __tablename__ = "generated_plans"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_generated_plans_version_positive"),
        CheckConstraint("case_count >= 0", name="ck_generated_plans_case_count_nonneg"),
        Index("ix_generated_plans_run", "run_id"),
    )

    run_id: int = Field(foreign_key="runs.id", nullable=False, index=True)

    format: PlanFormat = Field(
        sa_column=Column(
            PgEnum(PlanFormat, name="plan_format", create_type=True),
            nullable=False,
        )
    )
    body: str = Field(nullable=False)
    case_count: int = Field(nullable=False, default=0)
    version: int = Field(nullable=False, default=1)
    previous_plan_id: int | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("generated_plans.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )


class PlanTestCase(TimestampedBase, table=True):
    __tablename__ = "plan_test_cases"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_plan_test_cases_position_nonneg"),
        Index("ix_plan_test_cases_plan_position", "plan_id", "position"),
    )

    plan_id: int = Field(
        sa_column=Column(
            ForeignKey("generated_plans.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    position: int = Field(nullable=False, default=0)
    title: str = Field(nullable=False, max_length=512)
    body: str = Field(nullable=False)
    category: str | None = Field(default=None, max_length=64, index=True)
