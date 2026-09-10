"""What counts as "this ticket already has a plan".

``has_successful_test_plan`` is the watcher's never-regenerate guard
(``_screen`` → ``SKIP_ALREADY_PLANNED``). Because the answer is permanent —
a ticket that has a plan never gets another unattended one — the guard has to
be strict about what a plan *is*. A row with zero cases would retire the
ticket from unattended generation while leaving the tester a header and
nothing to test.

Zero-case rows are not hypothetical. ``quarantine_ungrounded_cases`` pulls
ungrounded cases out of the graded sections before persistence, so a plan
whose every case was quarantined stores as ``case_count = 0``; and test runs
that reached the production database before ``tests/conftest.py`` blocked them
left zero-case plans on live tickets (SK-2563, SK-2609, SK-2700).

These tests read the compiled SQL rather than a database, which is the honest
level for a repository whose only logic *is* the query: the suite has no
Postgres to run against, and the ARRAY/JSONB columns rule out SQLite.
"""
from __future__ import annotations

import pytest

from src.app.repositories import plan_repository


class _CapturingSession:
    """Records the statement handed to ``exec`` and returns nothing."""

    def __init__(self):
        self.statements = []

    async def exec(self, statement):
        self.statements.append(statement)
        return _EmptyResult()


class _EmptyResult:
    def first(self):
        return None

    def all(self):
        return []


def _sql(statement) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
async def test_a_plan_with_no_cases_does_not_count_as_planned():
    session = _CapturingSession()
    await plan_repository.has_successful_test_plan(session, ticket_key="SK-2609")

    sql = _sql(session.statements[0])
    assert "case_count > 0" in sql, (
        "an empty plan must not satisfy the never-regenerate guard; SQL was:\n" + sql
    )


@pytest.mark.asyncio
async def test_the_guard_still_requires_a_successful_test_plan_run():
    """The case_count filter is an addition, not a replacement — a plan from a
    failed run, or from a Bug Lens run, still must not count."""
    session = _CapturingSession()
    await plan_repository.has_successful_test_plan(session, ticket_key="SK-2609")

    sql = _sql(session.statements[0])
    assert "status" in sql
    assert "run_type" in sql
    assert "test_plan" in sql


@pytest.mark.asyncio
async def test_the_version_history_still_shows_empty_plans():
    """The guard ignores empty plans; the history banner must not. A tester
    looking at why their ticket has four versions needs to see all four —
    that visibility is what surfaced the test-suite pollution in the first
    place."""
    session = _CapturingSession()
    await plan_repository.list_runs_with_plans_by_ticket(
        session, ticket_key="SK-2609"
    )

    assert "case_count > 0" not in _sql(session.statements[0])
