from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING

from src.app.db import crud
from src.app.db.base import utcnow
from src.app.db.models.plan import GeneratedPlan, PlanFormat, PlanTestCase
from src.app.db.models.run import Run, RunStatus, RunType
from src.app.db.mongo import transaction

_TEST_PLAN_RUN_TYPES = (RunType.test_plan, RunType.test_plan_multi)

# The SQL version expressed this as a join against `runs`. Mongo has no join in
# the query language, so the equivalent is: resolve the matching run ids first,
# then filter plans by `run_id: {$in: ...}`. Two round trips instead of one, but
# `runs` is small and indexed on `ticket_keys`, and it keeps the intent readable
# next to the original.
_SUCCESSFUL_TEST_PLAN_RUN = {
    "status": RunStatus.ok.value,
    "run_type": {"$in": [t.value for t in _TEST_PLAN_RUN_TYPES]},
}


async def _run_ids_for_ticket(
    db: AsyncIOMotorDatabase,
    ticket_key: str,
    *,
    successful_only: bool = True,
) -> list[int]:
    """Ids of runs whose `ticket_keys` array contains `ticket_key`.

    An array-membership match is the one place Mongo is a straight improvement
    here: `{"ticket_keys": key}` matches an element directly, where Postgres
    needed the `@>` containment operator over a text[] column.
    """
    filter_: dict = {"ticket_keys": ticket_key}
    if successful_only:
        filter_.update(_SUCCESSFUL_TEST_PLAN_RUN)
    else:
        filter_["run_type"] = {"$in": [t.value for t in _TEST_PLAN_RUN_TYPES]}
    cursor = db[Run.__collection__].find(filter_, {"_id": 1})
    return [d["_id"] for d in await cursor.to_list(length=None)]


async def save_with_cases(
    db: AsyncIOMotorDatabase,
    *,
    run_id: int,
    format: PlanFormat,
    body: str,
    cases: Iterable[tuple[str, str, str | None]],
    version: int = 1,
    previous_plan_id: int | None = None,
) -> GeneratedPlan:
    cases_list = list(cases)
    plan = GeneratedPlan(
        run_id=run_id,
        format=format,
        body=body,
        case_count=len(cases_list),
        version=version,
        previous_plan_id=previous_plan_id,
    )
    async with transaction() as session:
        await crud.insert(db, plan, session=session)
        for position, (title, body_text, category) in enumerate(cases_list):
            await crud.insert(
                db,
                PlanTestCase(
                    plan_id=plan.id,
                    position=position,
                    title=title[:512],
                    body=body_text,
                    category=category,
                ),
                session=session,
            )
    return plan


async def find_latest_plan_for_ticket(
    db: AsyncIOMotorDatabase,
    *,
    ticket_key: str,
    exclude_run_id: int | None = None,
) -> GeneratedPlan | None:
    """Return the most recent successful test-plan GeneratedPlan whose run touched
    `ticket_key`, or None. Used to chain regenerations via previous_plan_id."""
    run_ids = await _run_ids_for_ticket(db, ticket_key)
    if exclude_run_id is not None:
        run_ids = [r for r in run_ids if r != exclude_run_id]
    if not run_ids:
        return None
    return await crud.find_one(
        db,
        GeneratedPlan,
        {"run_id": {"$in": run_ids}},
        sort=[("created_at", DESCENDING)],
    )


async def list_runs_with_plans_by_ticket(
    db: AsyncIOMotorDatabase,
    *,
    ticket_key: str,
    limit: int = 20,
) -> list[dict]:
    """Return run+plan summary rows for every successful test-plan run that
    touched `ticket_key`, newest first. Lightweight payload for the history banner."""
    runs = await crud.find_many(
        db,
        Run,
        {"ticket_keys": ticket_key, **_SUCCESSFUL_TEST_PLAN_RUN},
        sort=[("created_at", DESCENDING)],
    )
    if not runs:
        return []

    plans = await crud.find_many(
        db, GeneratedPlan, {"run_id": {"$in": [r.id for r in runs]}}
    )
    plans_by_run: dict[int, list[GeneratedPlan]] = {}
    for plan in plans:
        plans_by_run.setdefault(plan.run_id, []).append(plan)

    # The SQL applied LIMIT after the join, so a run with two plans consumed two
    # rows of the budget. Build the joined pairs in run order, then truncate, so
    # the banner shows the same entries it did on Postgres.
    rows: list[dict] = []
    for run in runs:
        for plan in plans_by_run.get(run.id, []):
            rows.append(
                {
                    "run_id": run.id,
                    "run_type": run.run_type.value,
                    "created_at": run.created_at.isoformat() if run.created_at else None,
                    "model": run.model,
                    "ticket_keys": list(run.ticket_keys or []),
                    "plan_id": plan.id,
                    "case_count": plan.case_count,
                    "version": plan.version,
                    "previous_plan_id": plan.previous_plan_id,
                    "jira_comment_id": plan.jira_comment_id,
                    "posted_at": plan.posted_at.isoformat() if plan.posted_at else None,
                }
            )
            if len(rows) >= limit:
                return rows
    return rows


async def mark_plan_posted_to_jira(
    db: AsyncIOMotorDatabase,
    *,
    plan_id: int,
    ticket_key: str,
    jira_comment_id: str,
) -> None:
    """Mark `plan_id` as the version currently live in Jira for `ticket_key`,
    and clear the same fields on every other plan whose run touched that ticket.

    Posting is update-in-place on Jira's side, so at most one plan per ticket
    can be "live" at a time — superseded versions must be cleared, not kept.

    The clear and the set run in one transaction where the deployment supports it
    (Atlas does). On a standalone dev Mongo they are two statements, and a crash
    between them would briefly leave the ticket with no live plan — recoverable by
    re-posting, and the reason the local dev target is a replica set too.
    """
    now = utcnow()
    run_ids = await _run_ids_for_ticket(db, ticket_key, successful_only=False)

    async with transaction() as session:
        if run_ids:
            await db[GeneratedPlan.__collection__].update_many(
                {
                    "_id": {"$ne": plan_id},
                    "jira_comment_id": {"$ne": None},
                    "run_id": {"$in": run_ids},
                },
                {"$set": {"jira_comment_id": None, "posted_at": None, "updated_at": now}},
                session=session,
            )
        await db[GeneratedPlan.__collection__].update_one(
            {"_id": plan_id},
            {
                "$set": {
                    "jira_comment_id": jira_comment_id,
                    "posted_at": now,
                    "updated_at": now,
                }
            },
            session=session,
        )


async def get_plan_with_cases(
    db: AsyncIOMotorDatabase,
    *,
    plan_id: int,
) -> tuple[GeneratedPlan, list[PlanTestCase]] | None:
    plan = await crud.get_by_id(db, GeneratedPlan, plan_id)
    if plan is None:
        return None
    cases = await crud.find_many(
        db,
        PlanTestCase,
        {"plan_id": plan_id},
        sort=[("position", ASCENDING)],
    )
    return plan, cases


async def find_last_test_plan_attempt_at(
    db: AsyncIOMotorDatabase,
    *,
    ticket_key: str,
) -> datetime | None:
    """When a test plan was last *attempted* for `ticket_key`, success or not.

    Distinct from ``list_runs_with_plans_by_ticket``, which inner-joins
    GeneratedPlan and so only ever sees runs that produced a plan. The
    queue watcher needs failed attempts too: a ticket whose generation
    times out every cycle would otherwise be retried forever, and each
    retry costs a full Opus call.
    """
    run = await crud.find_one(
        db,
        Run,
        {
            "ticket_keys": ticket_key,
            "run_type": {"$in": [t.value for t in _TEST_PLAN_RUN_TYPES]},
        },
        sort=[("created_at", DESCENDING)],
    )
    return run.created_at if run else None


async def has_successful_test_plan(
    db: AsyncIOMotorDatabase,
    *,
    ticket_key: str,
) -> bool:
    """Whether `ticket_key` already has a stored plan worth reusing.

    A plan with no cases does not count. The document's existence is what the
    watcher's never-regenerate guard keys off, so an empty plan would retire
    the ticket from unattended generation forever while giving the tester
    nothing to test — the plan they'd inherit is a header and no cases.

    Empty plans are real: the pipeline quarantines ungrounded cases out of
    the graded sections (``quarantine_ungrounded_cases``), and a plan whose
    every case was quarantined persists as zero cases. Test runs that
    reached the old Postgres database before ``tests/conftest.py`` blocked
    them left zero-case plans on live tickets too.
    """
    run_ids = await _run_ids_for_ticket(db, ticket_key)
    if not run_ids:
        return False
    doc = await db[GeneratedPlan.__collection__].find_one(
        {"run_id": {"$in": run_ids}, "case_count": {"$gt": 0}}, {"_id": 1}
    )
    return doc is not None
