"""End-to-end exercise of every repository against an in-memory MongoDB.

Why this file exists
--------------------
The Postgres-to-Mongo cutover rewrote all nine repositories, and the queries it
replaced were the kind SQL checks for you: a typo'd column name failed loudly at
the database. Mongo does not do that — a filter naming a field that does not
exist simply matches nothing, so a mistranslated query reads as "no results"
rather than as an error. `has_successful_test_plan` returning a silent False is
exactly the shape that would turn the watcher's never-regenerate guard into
"regenerate forever".

So these tests run the real repository functions against a real (if in-memory)
Mongo and assert on the data that comes back, rather than on the filters that go
in. `tests/test_plan_repository.py` still asserts on the filters, because the
conditions it guards are policy; this file checks the plumbing underneath.

The fixture writes the mock handle straight into the module globals, bypassing
`init_client` — which `conftest.py` has already replaced with a raiser — so these
tests never reach a real cluster.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from mongomock_motor import AsyncMongoMockClient

from src.app.db import mongo as db_mongo
from src.app.db.models.plan import PlanFormat
from src.app.db.models.run import RunStatus, RunType
from src.app.repositories import (
    bug_analysis_repository,
    jira_ticket_repository,
    plan_repository,
    run_repository,
    test_plan_progress_repository,
    ticket_hold_repository,
    user_repository,
    walkthrough_repository,
)


@pytest.fixture
def db(monkeypatch):
    client = AsyncMongoMockClient()
    database = client["testplan_test"]
    monkeypatch.setattr(db_mongo, "_client", client)
    monkeypatch.setattr(db_mongo, "_db", database)
    # mongomock is a standalone, so the transaction helper degrades to no session.
    monkeypatch.setattr(db_mongo, "_supports_transactions", False)
    return database


def _analysis(**overrides):
    """A BugAnalysis with every required field filled in.

    The dataclass has nine required fields; these tests care about two or three
    of them, so the rest are defaulted here rather than restated per test.
    """
    from src.app.models import BugAnalysis

    base = dict(
        bug_summary="It breaks",
        root_cause="A typo",
        fix_status="fixed",
        fix_explanation="corrected the typo",
        regression_tests=[],
        similar_patterns=[],
        fix_complexity="low",
        fix_effort_estimate="1h",
        fix_complexity_reasoning="one line",
    )
    base.update(overrides)
    return BugAnalysis(**base)


async def _make_run(db, ticket_key="SK-2609", *, status=RunStatus.ok, run_type=RunType.test_plan):
    user = await user_repository.get_or_create_by_email(db, email="qa@example.com")
    return await run_repository.create(
        db,
        user_id=user.id,
        run_type=run_type,
        ticket_keys=[ticket_key],
        model="claude-opus-5",
        llm_provider="claude",
        status=status,
    )


@pytest.mark.asyncio
async def test_ids_are_integers_and_increment(db):
    """The frontend, the API routes and previous_plan_id all pass ids as ints.
    ObjectIds would have broken every one of those contracts."""
    first = await _make_run(db)
    second = await _make_run(db, "SK-2700")
    assert isinstance(first.id, int)
    assert second.id == first.id + 1


@pytest.mark.asyncio
async def test_a_user_is_reused_rather_than_duplicated(db):
    one = await user_repository.get_or_create_by_email(db, email="qa@example.com")
    two = await user_repository.get_or_create_by_email(
        db, email="qa@example.com", display_name="QA"
    )
    assert one.id == two.id
    assert two.display_name == "QA"


@pytest.mark.asyncio
async def test_a_plan_and_its_cases_round_trip(db):
    run = await _make_run(db)
    plan = await plan_repository.save_with_cases(
        db,
        run_id=run.id,
        format=PlanFormat.markdown,
        body="# Plan",
        cases=[("Case A", "do a", "happy_path"), ("Case B", "do b", "edge_cases")],
    )
    assert plan.case_count == 2

    result = await plan_repository.get_plan_with_cases(db, plan_id=plan.id)
    assert result is not None
    stored, cases = result
    assert stored.body == "# Plan"
    assert [c.title for c in cases] == ["Case A", "Case B"]
    # Ordering is by `position`, which the history view and the Jira comment rely on.
    assert [c.position for c in cases] == [0, 1]
    assert cases[1].category == "edge_cases"


@pytest.mark.asyncio
async def test_an_empty_plan_does_not_satisfy_the_regenerate_guard(db):
    """The watcher never regenerates, so a zero-case plan must not retire the
    ticket — otherwise the tester inherits a header and nothing to test."""
    run = await _make_run(db)
    await plan_repository.save_with_cases(
        db, run_id=run.id, format=PlanFormat.markdown, body="# Plan", cases=[]
    )
    assert await plan_repository.has_successful_test_plan(db, ticket_key="SK-2609") is False


@pytest.mark.asyncio
async def test_a_real_plan_does_satisfy_the_regenerate_guard(db):
    run = await _make_run(db)
    await plan_repository.save_with_cases(
        db,
        run_id=run.id,
        format=PlanFormat.markdown,
        body="# Plan",
        cases=[("Case A", "do a", "happy_path")],
    )
    assert await plan_repository.has_successful_test_plan(db, ticket_key="SK-2609") is True


@pytest.mark.asyncio
async def test_a_failed_run_never_counts_as_planned(db):
    run = await _make_run(db, status=RunStatus.error)
    await plan_repository.save_with_cases(
        db,
        run_id=run.id,
        format=PlanFormat.markdown,
        body="# Plan",
        cases=[("Case A", "do a", None)],
    )
    assert await plan_repository.has_successful_test_plan(db, ticket_key="SK-2609") is False


@pytest.mark.asyncio
async def test_a_bug_lens_run_never_counts_as_planned(db):
    run = await _make_run(db, run_type=RunType.bug_lens)
    await plan_repository.save_with_cases(
        db,
        run_id=run.id,
        format=PlanFormat.markdown,
        body="# Plan",
        cases=[("Case A", "do a", None)],
    )
    assert await plan_repository.has_successful_test_plan(db, ticket_key="SK-2609") is False


@pytest.mark.asyncio
async def test_only_the_newest_plan_stays_live_in_jira(db):
    """Posting is update-in-place on Jira's side, so at most one version per
    ticket may carry a comment id. The superseded one must be cleared."""
    run = await _make_run(db)
    old = await plan_repository.save_with_cases(
        db, run_id=run.id, format=PlanFormat.markdown, body="v1", cases=[("A", "a", None)]
    )
    new = await plan_repository.save_with_cases(
        db, run_id=run.id, format=PlanFormat.markdown, body="v2", cases=[("B", "b", None)]
    )

    await plan_repository.mark_plan_posted_to_jira(
        db, plan_id=old.id, ticket_key="SK-2609", jira_comment_id="c1"
    )
    await plan_repository.mark_plan_posted_to_jira(
        db, plan_id=new.id, ticket_key="SK-2609", jira_comment_id="c2"
    )

    reloaded_old = (await plan_repository.get_plan_with_cases(db, plan_id=old.id))[0]
    reloaded_new = (await plan_repository.get_plan_with_cases(db, plan_id=new.id))[0]
    assert reloaded_old.jira_comment_id is None
    assert reloaded_old.posted_at is None
    assert reloaded_new.jira_comment_id == "c2"
    assert reloaded_new.posted_at is not None


@pytest.mark.asyncio
async def test_the_history_banner_lists_every_version_including_empty_ones(db):
    run = await _make_run(db)
    await plan_repository.save_with_cases(
        db, run_id=run.id, format=PlanFormat.markdown, body="v1", cases=[]
    )
    await plan_repository.save_with_cases(
        db, run_id=run.id, format=PlanFormat.markdown, body="v2", cases=[("A", "a", None)]
    )
    rows = await plan_repository.list_runs_with_plans_by_ticket(db, ticket_key="SK-2609")
    assert len(rows) == 2
    assert {r["case_count"] for r in rows} == {0, 1}


@pytest.mark.asyncio
async def test_a_ticket_with_no_runs_reports_no_prior_attempt(db):
    assert await plan_repository.find_last_test_plan_attempt_at(db, ticket_key="SK-9999") is None


@pytest.mark.asyncio
async def test_a_failed_attempt_still_counts_as_an_attempt(db):
    """The cooldown exists so a ticket that fails every cycle stops burning an
    Opus call each sweep — which only works if failures are visible here."""
    await _make_run(db, status=RunStatus.error)
    assert await plan_repository.find_last_test_plan_attempt_at(db, ticket_key="SK-2609") is not None


@pytest.mark.asyncio
async def test_the_auto_dispatch_claim_is_only_granted_once(db):
    first = await jira_ticket_repository.mark_auto_bug_analysis_dispatched(db, ticket_key="SK-2511")
    second = await jira_ticket_repository.mark_auto_bug_analysis_dispatched(db, ticket_key="SK-2511")
    assert first == second
    stored = await jira_ticket_repository.get_auto_bug_analysis_dispatched_at(db, ticket_key="SK-2511")
    assert stored == first


@pytest.mark.asyncio
async def test_a_ticket_snapshot_upserts_rather_than_duplicating(db):
    await jira_ticket_repository.upsert_snapshot(db, ticket_key="SK-1", title="First")
    await jira_ticket_repository.upsert_snapshot(db, ticket_key="SK-1", status="In Testing")
    docs = await db["jira_tickets"].count_documents({"ticket_key": "SK-1"})
    assert docs == 1
    ticket = await jira_ticket_repository.upsert_snapshot(db, ticket_key="SK-1")
    assert ticket.title == "First"
    assert ticket.status == "In Testing"


@pytest.mark.asyncio
async def test_a_hold_is_editable_without_resetting_held_since(db):
    first = await ticket_hold_repository.upsert_hold(db, ticket_key="SK-1", reason="code-review")
    edited = await ticket_hold_repository.upsert_hold(
        db, ticket_key="SK-1", reason="dependency", note="waiting on SK-2"
    )
    assert edited.created_at == first.created_at
    assert edited.reason == "dependency"
    assert edited.note == "waiting on SK-2"


@pytest.mark.asyncio
async def test_resuming_a_ticket_removes_the_hold(db):
    await ticket_hold_repository.upsert_hold(db, ticket_key="SK-1", reason="other")
    assert await ticket_hold_repository.clear_hold(db, ticket_key="SK-1") is True
    assert await ticket_hold_repository.get_hold(db, ticket_key="SK-1") is None
    # Clearing a ticket that is not held is a no-op, not an error.
    assert await ticket_hold_repository.clear_hold(db, ticket_key="SK-1") is False


@pytest.mark.asyncio
async def test_progress_is_replaced_wholesale_and_deduped(db):
    await test_plan_progress_repository.upsert_progress(
        db, progress_key="sk-1:abc", checked_ids=["b", "a", "a"]
    )
    row = await test_plan_progress_repository.get_progress(db, progress_key="SK-1:ABC")
    assert row is not None
    assert row.checked_ids == '["a", "b"]'

    await test_plan_progress_repository.upsert_progress(
        db, progress_key="SK-1:ABC", checked_ids=["c"]
    )
    row = await test_plan_progress_repository.get_progress(db, progress_key="SK-1:ABC")
    assert row.checked_ids == '["c"]'
    assert await db["test_plan_progress"].count_documents({}) == 1


@pytest.mark.asyncio
async def test_a_walkthrough_survives_being_edited(db):
    await walkthrough_repository.upsert_walkthrough(
        db,
        ticket_key="SK-1",
        loom_url="https://loom.com/x",
        notes="set up a seller",
        screenshots=[{"filename": "one.png", "url": "https://jira/one.png"}],
    )
    row = await walkthrough_repository.get_walkthrough(db, ticket_key="SK-1")
    assert row.loom_url == "https://loom.com/x"
    assert walkthrough_repository.decode_screenshots(row) == [
        {"filename": "one.png", "url": "https://jira/one.png", "media_id": None}
    ]

    await walkthrough_repository.upsert_walkthrough(
        db, ticket_key="SK-1", loom_url=None, notes="new notes", screenshots=[]
    )
    row = await walkthrough_repository.get_walkthrough(db, ticket_key="SK-1")
    assert row.notes == "new notes"
    assert walkthrough_repository.decode_screenshots(row) == []
    assert await db["ticket_walkthroughs"].count_documents({}) == 1


@pytest.mark.asyncio
async def test_cost_survives_a_round_trip_exactly(db):
    """Postgres held this as NUMERIC(10, 6). A float would lose the last digits,
    which is the whole reason it is stored as Decimal128."""
    run = await _make_run(db)
    await run_repository.mark_completed(
        db, run=run, latency_ms=1200, prompt_tokens=10, output_tokens=20, cost_usd=Decimal("0.123456")
    )
    from src.app.db import crud
    from src.app.db.models.run import Run

    reloaded = await crud.get_by_id(db, Run, run.id)
    assert reloaded.cost_usd == Decimal("0.123456")
    assert reloaded.latency_ms == 1200
    assert reloaded.status == RunStatus.ok


@pytest.mark.asyncio
async def test_a_stored_bug_analysis_is_readable_back(db):
    """The read path exists so a watcher-run analysis can be displayed instead of
    silently re-run at the tester's expense."""
    run = await _make_run(db, "SK-2511", run_type=RunType.bug_lens)
    await bug_analysis_repository.save(
        db, run_id=run.id, analysis=_analysis(regression_tests=["check the thing"])
    )

    found = await bug_analysis_repository.find_latest_for_ticket(db, ticket_key="SK-2511")
    assert found is not None
    assert found["bug_summary"] == "It breaks"
    assert found["regression_tests"] == ["check the thing"]
    assert found["ticket_keys"] == ["SK-2511"]
    assert found["run_id"] == run.id


@pytest.mark.asyncio
async def test_regression_tests_seed_from_sibling_tickets_only(db):
    """Seeds come from tickets sharing a parent — and never from the ticket being
    analysed, which would just echo its own prior run back at it."""
    await jira_ticket_repository.upsert_snapshot(db, ticket_key="SK-10", parent_key="SK-100")
    await jira_ticket_repository.upsert_snapshot(db, ticket_key="SK-11", parent_key="SK-100")

    sibling_run = await _make_run(db, "SK-10", run_type=RunType.bug_lens)
    await bug_analysis_repository.save(
        db,
        run_id=sibling_run.id,
        analysis=_analysis(bug_summary="sibling", regression_tests=["sibling check"]),
    )

    seeds = await bug_analysis_repository.find_seed_regression_tests(
        db, ticket_key="SK-11", parent_key="SK-100"
    )
    assert [s["regression_tests"] for s in seeds] == [["sibling check"]]
    assert seeds[0]["source_ticket_keys"] == ["SK-10"]

    # Asking from the sibling's own perspective finds nothing: the only analysis
    # belongs to the ticket doing the asking.
    assert await bug_analysis_repository.find_seed_regression_tests(
        db, ticket_key="SK-10", parent_key="SK-100"
    ) == []


@pytest.mark.asyncio
async def test_an_analysis_with_no_regression_tests_is_not_offered_as_a_seed(db):
    await jira_ticket_repository.upsert_snapshot(db, ticket_key="SK-10", parent_key="SK-100")
    run = await _make_run(db, "SK-10", run_type=RunType.bug_lens)
    await bug_analysis_repository.save(
        db, run_id=run.id, analysis=_analysis(bug_summary="no tests", regression_tests=[])
    )
    assert await bug_analysis_repository.find_seed_regression_tests(
        db, ticket_key="SK-11", parent_key="SK-100"
    ) == []
