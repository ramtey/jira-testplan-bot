from __future__ import annotations

from collections.abc import Iterable

from sqlmodel import desc, select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.app.db.models.plan import GeneratedPlan, PlanFormat, PlanTestCase
from src.app.db.models.run import Run, RunStatus, RunType


async def save_with_cases(
    session: AsyncSession,
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
    session.add(plan)
    await session.flush()

    for position, (title, body_text, category) in enumerate(cases_list):
        session.add(
            PlanTestCase(
                plan_id=plan.id,
                position=position,
                title=title[:512],
                body=body_text,
                category=category,
            )
        )
    await session.flush()
    return plan


_TEST_PLAN_RUN_TYPES = (RunType.test_plan, RunType.test_plan_multi)


async def find_latest_plan_for_ticket(
    session: AsyncSession,
    *,
    ticket_key: str,
    exclude_run_id: int | None = None,
) -> GeneratedPlan | None:
    """Return the most recent successful test-plan GeneratedPlan whose run touched
    `ticket_key`, or None. Used to chain regenerations via previous_plan_id."""
    stmt = (
        select(GeneratedPlan)
        .join(Run, Run.id == GeneratedPlan.run_id)
        .where(Run.ticket_keys.contains([ticket_key]))
        .where(Run.status == RunStatus.ok)
        .where(Run.run_type.in_(_TEST_PLAN_RUN_TYPES))
        .order_by(desc(GeneratedPlan.created_at))
        .limit(1)
    )
    if exclude_run_id is not None:
        stmt = stmt.where(GeneratedPlan.run_id != exclude_run_id)
    result = await session.exec(stmt)
    return result.first()


async def list_runs_with_plans_by_ticket(
    session: AsyncSession,
    *,
    ticket_key: str,
    limit: int = 20,
) -> list[dict]:
    """Return run+plan summary rows for every successful test-plan run that
    touched `ticket_key`, newest first. Lightweight payload for the history banner."""
    stmt = (
        select(Run, GeneratedPlan)
        .join(GeneratedPlan, GeneratedPlan.run_id == Run.id)
        .where(Run.ticket_keys.contains([ticket_key]))
        .where(Run.status == RunStatus.ok)
        .where(Run.run_type.in_(_TEST_PLAN_RUN_TYPES))
        .order_by(desc(Run.created_at))
        .limit(limit)
    )
    rows = (await session.exec(stmt)).all()
    return [
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
        }
        for run, plan in rows
    ]


async def get_plan_with_cases(
    session: AsyncSession,
    *,
    plan_id: int,
) -> tuple[GeneratedPlan, list[PlanTestCase]] | None:
    plan = await session.get(GeneratedPlan, plan_id)
    if plan is None:
        return None
    cases_stmt = (
        select(PlanTestCase)
        .where(PlanTestCase.plan_id == plan_id)
        .order_by(PlanTestCase.position)
    )
    cases = (await session.exec(cases_stmt)).all()
    return plan, list(cases)
