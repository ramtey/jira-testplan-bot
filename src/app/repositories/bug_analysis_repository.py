from __future__ import annotations

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
