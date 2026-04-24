from __future__ import annotations

from collections.abc import Iterable

from sqlmodel.ext.asyncio.session import AsyncSession

from src.app.db.models.plan import GeneratedPlan, PlanFormat, PlanTestCase


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
