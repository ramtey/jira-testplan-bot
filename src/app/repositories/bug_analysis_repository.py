from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import DESCENDING

from src.app.db import crud
from src.app.db.models.bug_analysis import BugAnalysisRecord
from src.app.db.models.jira_ticket import JiraTicket
from src.app.db.models.run import Run, RunStatus
from src.app.models import BugAnalysis


async def save(
    db: AsyncIOMotorDatabase,
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
    return await crud.insert(db, record)


async def _ticket_keys_by_run(
    db: AsyncIOMotorDatabase, run_ids: list[int]
) -> dict[int, list[str]]:
    cursor = db[Run.__collection__].find(
        {"_id": {"$in": run_ids}}, {"_id": 1, "ticket_keys": 1}
    )
    return {d["_id"]: list(d.get("ticket_keys") or []) for d in await cursor.to_list(length=None)}


async def find_seed_regression_tests(
    db: AsyncIOMotorDatabase,
    *,
    ticket_key: str,
    parent_key: str,
    limit: int = 5,
) -> list[dict]:
    """Find prior Bug Lens regression tests for tickets sharing a parent.

    Returns a list of dicts with shape:
        {"source_ticket_keys": list[str], "regression_tests": list[str], "created_at": datetime}

    Excludes the current ticket. Ordered by recency. Only documents whose
    `regression_tests` is a non-empty array are returned.

    The Postgres version was one raw statement leaning on `jsonb_array_length`,
    `ANY(...)` and a correlated EXISTS. Mongo has no join, so the same question is
    asked in three indexed steps: sibling tickets, then their runs, then those
    runs' analyses. The `regression_tests.0` test is how Mongo spells "array with
    at least one element" — it uses the index, where `$size: {$gt: 0}` would not.
    """
    sibling_cursor = db[JiraTicket.__collection__].find(
        {"parent_key": parent_key, "ticket_key": {"$ne": ticket_key}},
        {"ticket_key": 1},
    )
    siblings = [d["ticket_key"] for d in await sibling_cursor.to_list(length=None)]
    if not siblings:
        return []

    run_cursor = db[Run.__collection__].find({"ticket_keys": {"$in": siblings}}, {"_id": 1})
    run_ids = [d["_id"] for d in await run_cursor.to_list(length=None)]
    if not run_ids:
        return []

    records = await crud.find_many(
        db,
        BugAnalysisRecord,
        {"run_id": {"$in": run_ids}, "regression_tests.0": {"$exists": True}},
        sort=[("created_at", DESCENDING)],
        limit=limit,
    )
    if not records:
        return []

    keys_by_run = await _ticket_keys_by_run(db, [r.run_id for r in records])
    return [
        {
            "source_ticket_keys": keys_by_run.get(record.run_id, []),
            "regression_tests": list(record.regression_tests or []),
            "created_at": record.created_at,
        }
        for record in records
    ]


# Fields returned by `find_latest_for_ticket`. Ordered to match the
# BugAnalysis dataclass so the route can rebuild the same response shape
# `/bug-lens/analyze` returns — the UI renders one component for both.
_ANALYSIS_FIELDS = (
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
    db: AsyncIOMotorDatabase,
    *,
    ticket_key: str,
) -> dict | None:
    """Return the newest stored Bug Lens analysis touching `ticket_key`.

    Exists so a Bug Lens run that happened outside the current browser
    session — the queue watcher's, or a teammate's — can be *displayed*
    instead of silently re-run. Analyses were persisted from the start but
    had no read path, so the watcher was paying for an analysis nobody could
    see and the tester paid again by clicking Analyze.

    Includes multi-ticket analyses, since those documents apply to every key in
    the run's `ticket_keys`.
    """
    key = ticket_key.upper()
    run_cursor = db[Run.__collection__].find(
        {"ticket_keys": key, "status": RunStatus.ok.value}, {"_id": 1}
    )
    run_ids = [d["_id"] for d in await run_cursor.to_list(length=None)]
    if not run_ids:
        return None

    record = await crud.find_one(
        db,
        BugAnalysisRecord,
        {"run_id": {"$in": run_ids}},
        sort=[("created_at", DESCENDING)],
    )
    if record is None:
        return None

    analysis = {field: getattr(record, field) for field in _ANALYSIS_FIELDS}
    # These list fields are stored as None when empty; the dataclass-derived
    # response uses [] for regression_tests / similar_patterns, so match it.
    for field in ("regression_tests", "similar_patterns"):
        analysis[field] = list(analysis[field] or [])

    keys_by_run = await _ticket_keys_by_run(db, [record.run_id])
    return {
        **analysis,
        "run_id": record.run_id,
        "ticket_keys": keys_by_run.get(record.run_id, []),
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }
