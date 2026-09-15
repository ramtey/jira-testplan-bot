"""A plan that was never persisted must not look like one that was.

``start_run`` deliberately keeps generating when the database is unreachable —
a Mongo blip should not cost the user their plan. What it must not do is stay
silent about it. It returned ``RunContext(run_id=None)`` and logged, so
``complete_with_plan`` returned None, ``generate_single`` simply omitted
``plan_id``, and the response was otherwise indistinguishable from a persisted
one. The caller rendered the plan and could post it to Jira, and nothing
anywhere said the plan had no run, no id and therefore no progress key.

That is how SK-2342's plan reached comment 330141 on 2026-09-15 with no run and
no plan row: a transient Atlas outage, reported as success. Recovering it needed
``plan_adoption`` to parse the plan back out of its own Jira comment.

These tests pin the signal, not the swallow: generation still succeeds, and it
says when nothing recorded it.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.app.db.models.plan import PlanFormat
from src.app.db.models.run import RunType
from src.app.services import run_tracker


class _Unreachable(Exception):
    """Stands in for pymongo's ServerSelectionTimeoutError."""


@pytest.mark.asyncio
async def test_an_unreachable_database_still_returns_a_context():
    """Generation must survive the outage — the user still gets their plan."""
    with patch.object(run_tracker, "get_db", side_effect=_Unreachable("No servers found yet")):
        ctx = await run_tracker.start_run(
            run_type=RunType.test_plan,
            ticket_keys=["SK-2342"],
            model="claude-opus-5",
            llm_provider="anthropic",
        )
    assert ctx.run_id is None


@pytest.mark.asyncio
async def test_an_unreachable_database_records_why_it_could_not_persist():
    """The bit that was missing. Without this the caller cannot tell a plan with
    no id from a plan that simply has not been saved yet."""
    with patch.object(run_tracker, "get_db", side_effect=_Unreachable("No servers found yet")):
        ctx = await run_tracker.start_run(
            run_type=RunType.test_plan,
            ticket_keys=["SK-2342"],
            model="claude-opus-5",
            llm_provider="anthropic",
        )
    assert ctx.failure is not None
    assert "No servers found yet" in ctx.failure
    assert "_Unreachable" in ctx.failure


@pytest.mark.asyncio
async def test_completing_a_run_that_never_started_stays_a_no_op():
    ctx = run_tracker.RunContext(run_id=None, failure="boom")
    saved = await run_tracker.complete_with_plan(
        ctx,
        plan_body="{}",
        plan_format=PlanFormat.json,
        cases=[],
    )
    assert saved is None
    # The original cause survives; it is not overwritten by the later no-op.
    assert ctx.failure == "boom"


@pytest.mark.asyncio
async def test_a_plan_write_that_fails_late_is_also_recorded():
    """The database can go away between ``start_run`` and the plan write. That
    run exists but has no plan, which is just as untracked from the caller's
    side."""
    ctx = run_tracker.RunContext(run_id=42)
    with patch.object(run_tracker, "get_db", side_effect=_Unreachable("connection reset")):
        saved = await run_tracker.complete_with_plan(
            ctx,
            plan_body="{}",
            plan_format=PlanFormat.json,
            cases=[],
        )
    assert saved is None
    assert ctx.failure is not None
    assert "connection reset" in ctx.failure
