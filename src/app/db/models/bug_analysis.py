from __future__ import annotations

from typing import Any

from sqlalchemy import Column, ForeignKey, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from src.app.db.base import TimestampedBase


class BugAnalysisRecord(TimestampedBase, table=True):
    """Persisted Bug Lens analysis output for a run.

    One row per Bug Lens run. Multi-ticket runs share a single row that
    applies to all tickets in `runs.ticket_keys`.
    """

    __tablename__ = "bug_analyses"
    __table_args__ = (
        Index("ix_bug_analyses_run", "run_id"),
    )

    run_id: int = Field(
        sa_column=Column(
            ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )

    bug_summary: str = Field(nullable=False)
    root_cause: str | None = Field(default=None)
    fix_status: str = Field(nullable=False, max_length=32)
    fix_explanation: str | None = Field(default=None)
    fix_complexity: str | None = Field(default=None, max_length=32)
    fix_effort_estimate: str | None = Field(default=None, max_length=64)
    fix_complexity_reasoning: str | None = Field(default=None)
    why_tests_miss: str | None = Field(default=None)
    is_regression: bool | None = Field(default=None)
    regression_introduced_by: str | None = Field(default=None, max_length=512)

    regression_tests: list[str] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    similar_patterns: list[str] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    affected_flow: list[str] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    scope_of_impact: list[str] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    assumptions: list[str] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    open_questions: list[str] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    suspect_symbols: list[str] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    code_evidence: list[dict[str, Any]] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
