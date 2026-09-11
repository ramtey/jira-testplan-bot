"""What counts as "this ticket already has a plan".

``has_successful_test_plan`` is the watcher's never-regenerate guard
(``_screen`` → ``SKIP_ALREADY_PLANNED``). Because the answer is permanent —
a ticket that has a plan never gets another unattended one — the guard has to
be strict about what a plan *is*. A document with zero cases would retire the
ticket from unattended generation while leaving the tester a header and
nothing to test.

Zero-case documents are not hypothetical. ``quarantine_ungrounded_cases`` pulls
ungrounded cases out of the graded sections before persistence, so a plan
whose every case was quarantined stores as ``case_count = 0``; and test runs
that reached the production database before ``tests/conftest.py`` blocked them
left zero-case plans on live tickets (SK-2563, SK-2609, SK-2700).

These tests read the query filters the repository builds rather than running a
database, which is the honest level for a repository whose only logic *is* the
query: the suite has no Mongo to run against. Before the Mongo cutover the same
tests asserted against compiled SQL strings; asserting on the filter documents
checks the same conditions without the string matching.
"""
from __future__ import annotations

import pytest

from src.app.repositories import plan_repository


class _CapturingCollection:
    """Records every filter handed to ``find``/``find_one`` for one collection."""

    def __init__(self, name: str, recorder: list, docs: list[dict]):
        self._name = name
        self._recorder = recorder
        self._docs = docs

    def find(self, filter_=None, projection=None, **kwargs):
        self._recorder.append((self._name, filter_ or {}))
        return _Cursor(self._docs)

    async def find_one(self, filter_=None, projection=None, **kwargs):
        self._recorder.append((self._name, filter_ or {}))
        return self._docs[0] if self._docs else None


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    async def to_list(self, length=None):
        return list(self._docs)


class _CapturingDatabase:
    """Stands in for the Motor database handle.

    Returns one plausible document per collection so the repository proceeds past
    its early-outs and issues every query under test — a repository that returned
    no runs would short-circuit before ever filtering plans.
    """

    def __init__(self):
        self.queries: list[tuple[str, dict]] = []
        self._docs = {
            "runs": [
                {
                    "_id": 1,
                    "ticket_keys": ["SK-2609"],
                    "status": "ok",
                    "run_type": "test_plan",
                    "model": "claude-opus-5",
                    "llm_provider": "claude",
                    "user_id": 1,
                }
            ],
            "generated_plans": [
                {
                    "_id": 10,
                    "run_id": 1,
                    "format": "markdown",
                    "body": "",
                    "case_count": 0,
                    "version": 1,
                }
            ],
        }

    def __getitem__(self, name):
        return _CapturingCollection(name, self.queries, self._docs.get(name, []))

    def filters_for(self, collection: str) -> list[dict]:
        return [f for (name, f) in self.queries if name == collection]


@pytest.mark.asyncio
async def test_a_plan_with_no_cases_does_not_count_as_planned():
    db = _CapturingDatabase()
    await plan_repository.has_successful_test_plan(db, ticket_key="SK-2609")

    plan_filters = db.filters_for("generated_plans")
    assert plan_filters, "the guard never queried generated_plans"
    assert plan_filters[0].get("case_count") == {"$gt": 0}, (
        "an empty plan must not satisfy the never-regenerate guard; filter was:\n"
        f"{plan_filters[0]}"
    )


@pytest.mark.asyncio
async def test_the_guard_still_requires_a_successful_test_plan_run():
    """The case_count filter is an addition, not a replacement — a plan from a
    failed run, or from a Bug Lens run, still must not count."""
    db = _CapturingDatabase()
    await plan_repository.has_successful_test_plan(db, ticket_key="SK-2609")

    run_filters = db.filters_for("runs")
    assert run_filters, "the guard never constrained which runs it looked at"
    run_filter = run_filters[0]
    assert run_filter.get("status") == "ok"
    assert "test_plan" in run_filter.get("run_type", {}).get("$in", [])
    assert run_filter.get("ticket_keys") == "SK-2609"


@pytest.mark.asyncio
async def test_the_version_history_still_shows_empty_plans():
    """The guard ignores empty plans; the history banner must not. A tester
    looking at why their ticket has four versions needs to see all four —
    that visibility is what surfaced the test-suite pollution in the first
    place."""
    db = _CapturingDatabase()
    await plan_repository.list_runs_with_plans_by_ticket(db, ticket_key="SK-2609")

    for filter_ in db.filters_for("generated_plans"):
        assert "case_count" not in filter_, (
            "the history banner must not hide zero-case versions; filter was:\n"
            f"{filter_}"
        )
