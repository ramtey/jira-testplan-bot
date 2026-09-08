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
        suspect_locations=list(analysis.suspect_locations) if analysis.suspect_locations else None,
        blame_evidence=list(analysis.blame_evidence) if analysis.blame_evidence else None,
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


# Columns returned by `find_latest_for_ticket`. Ordered to match the
# BugAnalysis dataclass so the route can rebuild the same response shape
# `/bug-lens/analyze` returns — the UI renders one component for both.
_ANALYSIS_COLUMNS = (
    "bug_summary",
    "root_cause",
    "fix_status",
    "fix_explanation",
    "fix_complexity",
    "fix_effort_estimate",
    "fix_complexity_reasoning",
    "why_tests_miss",
    "is_regression",
    "regression_introduced_by",
    "regression_tests",
    "similar_patterns",
    "affected_flow",
    "scope_of_impact",
    "assumptions",
    "open_questions",
    "suspect_symbols",
    "code_evidence",
    "suspect_locations",
    "blame_evidence",
)


async def find_latest_for_ticket(
    session: AsyncSession,
    *,
    ticket_key: str,
) -> dict | None:
    """Return the newest stored Bug Lens analysis touching `ticket_key`.

    Exists so a Bug Lens run that happened outside the current browser
    session — the queue watcher's, or a teammate's — can be *displayed*
    instead of silently re-run. Analyses were persisted from the start but
    had no read path, so the watcher was paying for an analysis nobody could
    see and the tester paid again by clicking Analyze.

    Includes multi-ticket analyses, since those rows apply to every key in
    the run's `ticket_keys`.
    """
    sql = text(
        f"""
        SELECT r.ticket_keys, ba.created_at, ba.run_id,
               {", ".join(f"ba.{c}" for c in _ANALYSIS_COLUMNS)}
        FROM bug_analyses ba
        JOIN runs r ON r.id = ba.run_id
        WHERE :ticket_key = ANY(r.ticket_keys)
          AND r.status = 'ok'
        ORDER BY ba.created_at DESC
        LIMIT 1
        """
    )
    result = await session.execute(sql, {"ticket_key": ticket_key.upper()})
    row = result.first()
    if row is None:
        return None

    ticket_keys, created_at, run_id = row[0], row[1], row[2]
    analysis = dict(zip(_ANALYSIS_COLUMNS, row[3:]))
    # JSONB list columns come back as None when empty; the dataclass-derived
    # response uses [] for regression_tests / similar_patterns, so match it.
    for key in ("regression_tests", "similar_patterns"):
        analysis[key] = list(analysis[key] or [])
    return {
        **analysis,
        "run_id": run_id,
        "ticket_keys": list(ticket_keys or []),
        "created_at": created_at.isoformat() if created_at else None,
    }
