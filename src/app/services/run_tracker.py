from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from time import perf_counter

from src.app.db.models.plan import PlanFormat
from src.app.db.models.run import RunStatus, RunType
from src.app.db.session import get_sessionmaker
from src.app.repositories import (
    jira_ticket_repository,
    plan_repository,
    run_repository,
    user_repository,
)

logger = logging.getLogger(__name__)


@dataclass
class RunContext:
    """Handle passed between start_run and complete/fail calls.

    Holds the DB run id plus a monotonic start time so the route never has to
    manage timing itself. If `run_id` is None, DB recording was skipped (silent
    no-op) — all complete/fail calls become no-ops.
    """
    run_id: int | None
    started_at: float = field(default_factory=perf_counter)

    def elapsed_ms(self) -> int:
        return int((perf_counter() - self.started_at) * 1000)


def _actor_email() -> str:
    env_val = os.environ.get("JIRA_USERNAME")
    if env_val:
        return env_val
    from src.app.config import settings
    return settings.jira_username or "unknown@local"


async def start_run(
    *,
    run_type: RunType,
    ticket_keys: Iterable[str],
    model: str,
    llm_provider: str,
    had_pr_diff: bool = False,
    had_figma: bool = False,
    had_parent: bool = False,
    linked_ticket_count: int = 0,
    pr_count: int = 0,
    comment_count: int = 0,
    ticket_title: str | None = None,
    ticket_issue_type: str | None = None,
) -> RunContext:
    started = perf_counter()
    ticket_keys_list = list(ticket_keys)
    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            user = await user_repository.get_or_create_by_email(
                session, email=_actor_email()
            )
            for key in ticket_keys_list:
                await jira_ticket_repository.upsert_snapshot(
                    session,
                    ticket_key=key,
                    issue_type=ticket_issue_type,
                    title=ticket_title if len(ticket_keys_list) == 1 else None,
                )
            run = await run_repository.create(
                session,
                user_id=user.id,
                run_type=run_type,
                ticket_keys=ticket_keys_list,
                model=model,
                llm_provider=llm_provider,
                status=RunStatus.ok,
                had_pr_diff=had_pr_diff,
                had_figma=had_figma,
                had_parent=had_parent,
                linked_ticket_count=linked_ticket_count,
                pr_count=pr_count,
                comment_count=comment_count,
            )
            await session.commit()
            return RunContext(run_id=run.id, started_at=started)
    except Exception:
        logger.exception("run_tracker.start_run failed; continuing without DB recording")
        return RunContext(run_id=None, started_at=started)


async def complete_with_plan(
    ctx: RunContext,
    *,
    plan_body: str,
    plan_format: PlanFormat,
    cases: Iterable[tuple[str, str, str | None]],
    prompt_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: Decimal | float = 0,
) -> None:
    if ctx.run_id is None:
        return
    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            run = await session.get(_run_type(), ctx.run_id)
            if run is None:
                logger.warning("run_tracker.complete_with_plan: run_id=%s vanished", ctx.run_id)
                return
            await run_repository.mark_completed(
                session,
                run=run,
                latency_ms=ctx.elapsed_ms(),
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
            )

            # Chain regenerations on single-ticket runs: link to the prior plan
            # and bump version. Multi-ticket runs are intentionally left flat —
            # "previous" is ambiguous when several tickets fan in.
            previous_plan_id: int | None = None
            version = 1
            if run.ticket_keys and len(run.ticket_keys) == 1:
                prior = await plan_repository.find_latest_plan_for_ticket(
                    session,
                    ticket_key=run.ticket_keys[0],
                    exclude_run_id=ctx.run_id,
                )
                if prior is not None:
                    previous_plan_id = prior.id
                    version = (prior.version or 1) + 1

            await plan_repository.save_with_cases(
                session,
                run_id=ctx.run_id,
                format=plan_format,
                body=plan_body,
                cases=cases,
                previous_plan_id=previous_plan_id,
                version=version,
            )
            await session.commit()
    except Exception:
        logger.exception("run_tracker.complete_with_plan failed")


async def fail(ctx: RunContext, *, error_code: str) -> None:
    if ctx.run_id is None:
        return
    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            run = await session.get(_run_type(), ctx.run_id)
            if run is None:
                return
            await run_repository.mark_failed(
                session,
                run=run,
                error_code=error_code,
                latency_ms=ctx.elapsed_ms(),
            )
            await session.commit()
    except Exception:
        logger.exception("run_tracker.fail failed")


def _run_type():
    # Local import to avoid circular reference at module load time.
    from src.app.db.models.run import Run
    return Run
