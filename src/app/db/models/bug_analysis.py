from __future__ import annotations

from typing import Any, ClassVar

from src.app.db.base import DocumentBase


class BugAnalysisRecord(DocumentBase):
    """Persisted Bug Lens analysis output for a run.

    One document per Bug Lens run. Multi-ticket runs share a single document that
    applies to all tickets in ``runs.ticket_keys``.

    Every list field below was a Postgres JSONB column and is now a native BSON
    array, so these are queryable directly (``{"suspect_symbols": "foo"}``) instead
    of needing a JSONB containment operator.
    """

    __collection__: ClassVar[str] = "bug_analyses"

    run_id: int

    bug_summary: str
    root_cause: str | None = None
    fix_status: str
    fix_explanation: str | None = None
    fix_complexity: str | None = None
    fix_effort_estimate: str | None = None
    fix_complexity_reasoning: str | None = None
    why_tests_miss: str | None = None
    is_regression: bool | None = None
    regression_introduced_by: str | None = None

    regression_tests: list[str] | None = None
    similar_patterns: list[str] | None = None
    affected_flow: list[str] | None = None
    scope_of_impact: list[str] | None = None
    assumptions: list[str] | None = None
    open_questions: list[str] | None = None
    suspect_symbols: list[str] | None = None
    code_evidence: list[dict[str, Any]] | None = None
    suspect_locations: list[dict[str, Any]] | None = None
    blame_evidence: list[dict[str, Any]] | None = None
