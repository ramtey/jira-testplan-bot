from __future__ import annotations

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from src.app.db.models.bug_analysis import BugAnalysisRecord
from src.app.models import BugAnalysis


async def save(
    session: AsyncSession,
    *,
    run_id: int,
    analysis: BugAnalysis,
) -> BugAnalysisRecord:
    record = BugAnalysisRecord(
        run_id=run_id,
        bug_summary=analysis.bug_summary,
        root_cause=analysis.root_cause,
        fix_status=analysis.fix_status,
        fix_explanation=analysis.fix_explanation,
        fix_complexity=analysis.fix_complexity,
        fix_effort_estimate=analysis.fix_effort_estimate,
        fix_complexity_reasoning=analysis.fix_complexity_reasoning,
        why_tests_miss=analysis.why_tests_miss,
        is_regression=analysis.is_regression,
        regression_introduced_by=analysis.regression_introduced_by,
        regression_tests=list(analysis.regression_tests) if analysis.regression_tests else None,
        similar_patterns=list(analysis.similar_patterns) if analysis.similar_patterns else None,
        affected_flow=list(analysis.affected_flow) if analysis.affected_flow else None,
        scope_of_impact=list(analysis.scope_of_impact) if analysis.scope_of_impact else None,
        assumptions=list(analysis.assumptions) if analysis.assumptions else None,
        open_questions=list(analysis.open_questions) if analysis.open_questions else None,
        suspect_symbols=list(analysis.suspect_symbols) if analysis.suspect_symbols else None,
        code_evidence=list(analysis.code_evidence) if analysis.code_evidence else None,
    )
    session.add(record)
    await session.flush()
    return record


async def find_seed_regression_tests(
    session: AsyncSession,
    *,
    ticket_key: str,
    parent_key: str,
    limit: int = 5,
) -> list[dict]:
    """Find prior Bug Lens regression tests for tickets sharing a parent.

    Returns a list of dicts with shape:
        {"source_ticket_keys": list[str], "regression_tests": list[str], "created_at": datetime}

    Excludes the current ticket. Ordered by recency. Only rows whose
    `regression_tests` is a non-empty JSONB array are returned.
    """
    sql = text(
        """
        SELECT r.ticket_keys, ba.regression_tests, ba.created_at
        FROM bug_analyses ba
        JOIN runs r ON r.id = ba.run_id
        WHERE jsonb_typeof(ba.regression_tests) = 'array'
          AND jsonb_array_length(ba.regression_tests) > 0
          AND EXISTS (
            SELECT 1 FROM jira_tickets jt
            WHERE jt.ticket_key = ANY(r.ticket_keys)
              AND jt.parent_key = :parent_key
              AND jt.ticket_key <> :ticket_key
          )
        ORDER BY ba.created_at DESC
        LIMIT :limit
        """
    )
    result = await session.execute(
        sql,
        {"parent_key": parent_key, "ticket_key": ticket_key, "limit": limit},
    )
    rows: list[dict] = []
    for ticket_keys, regression_tests, created_at in result.all():
        rows.append(
            {
                "source_ticket_keys": list(ticket_keys or []),
                "regression_tests": list(regression_tests or []),
                "created_at": created_at,
            }
        )
    return rows
