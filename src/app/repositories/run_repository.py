from __future__ import annotations

from decimal import Decimal

from sqlmodel.ext.asyncio.session import AsyncSession

from src.app.db.models.run import Run, RunStatus, RunType


async def create(
    session: AsyncSession,
    *,
    user_id: int,
    run_type: RunType,
    ticket_keys: list[str],
    model: str,
    llm_provider: str,
    status: RunStatus = RunStatus.ok,
    had_pr_diff: bool = False,
    had_figma: bool = False,
    had_parent: bool = False,
    linked_ticket_count: int = 0,
    pr_count: int = 0,
    comment_count: int = 0,
) -> Run:
    run = Run(
        user_id=user_id,
        run_type=run_type,
        ticket_keys=ticket_keys,
        model=model,
        llm_provider=llm_provider,
        status=status,
        had_pr_diff=had_pr_diff,
        had_figma=had_figma,
        had_parent=had_parent,
        linked_ticket_count=linked_ticket_count,
        pr_count=pr_count,
        comment_count=comment_count,
    )
    session.add(run)
    await session.flush()
    return run


async def mark_completed(
    session: AsyncSession,
    *,
    run: Run,
    latency_ms: int,
    prompt_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: Decimal | float = Decimal("0"),
) -> Run:
    run.status = RunStatus.ok
    run.latency_ms = latency_ms
    run.prompt_tokens = prompt_tokens
    run.output_tokens = output_tokens
    run.cost_usd = Decimal(str(cost_usd))
    session.add(run)
    await session.flush()
    return run


async def mark_failed(
    session: AsyncSession,
    *,
    run: Run,
    error_code: str,
    latency_ms: int,
) -> Run:
    run.status = RunStatus.error
    run.error_code = error_code[:128]
    run.latency_ms = latency_ms
    session.add(run)
    await session.flush()
    return run
