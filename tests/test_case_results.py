"""Per-case results: who passed a case, and marks that cannot erase each other.

Why this file exists
--------------------
Progress used to be one stored set per progress key, replaced wholesale on every
save. Two things followed from that:

* **Nobody could tell who passed a case.** A runner's mark and a tester's tick
  were the same string in the same set, so a review of "what did the agent
  claim" had nothing to read.
* **Writers erased each other.** The browser saved its whole set 600 ms after a
  toggle. A runner writing between the browser's last poll and that save was
  silently undone — the mark existed for up to three seconds and then didn't.

Results are now appended one case at a time, each naming its actor, and the
checked set is derived from them on read. The legacy row is a frozen baseline:
marks made before this change keep showing, attributed to no one.
"""

from __future__ import annotations

import json

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from src.app import main
from src.app.config import settings
from src.app.db import crud
from src.app.db import mongo as db_mongo
from src.app.db.models.case_result import CaseResult
from src.app.db.models.plan import PlanFormat
from src.app.db.models.run import RunStatus, RunType
from src.app.db.models.test_plan_progress import TestPlanProgress as LegacyProgressRow
from src.app.repositories import (
    plan_repository,
    run_repository,
    test_plan_progress_repository,
    user_repository,
)
from src.app.repositories.test_plan_progress_repository import fold
from src.app.services import mark_carryover
from src.app.services.progress_key import build_progress_key

BODY = {
    "happy_path": [{"title": "Saves a property"}, {"title": "Shares a link"}],
    "edge_cases": [{"title": "Rejects a malformed token"}],
    "integration_tests": [],
    "regression_checklist": ["Login still works"],
}
KEY = build_progress_key(["SK-1"], json.dumps(BODY))


@pytest.fixture
def db(monkeypatch):
    client = AsyncMongoMockClient()
    database = client["testplan_test"]
    monkeypatch.setattr(db_mongo, "_client", client)
    monkeypatch.setattr(db_mongo, "_db", database)
    monkeypatch.setattr(db_mongo, "_supports_transactions", False)
    monkeypatch.setattr(settings, "qa_actor_email", "")
    return database


@pytest.fixture
def api(db):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    )


async def _save_plan(db, body=BODY, ticket_key="SK-1"):
    user = await user_repository.get_or_create_by_email(db, email="service@example.com")
    run = await run_repository.create(
        db, user_id=user.id, run_type=RunType.test_plan, ticket_keys=[ticket_key],
        model="claude-opus-5", llm_provider="claude", status=RunStatus.ok,
    )
    return await plan_repository.save_with_cases(
        db, run_id=run.id, format=PlanFormat.json, body=json.dumps(body), cases=[]
    )


async def _legacy(db, key, ids):
    await crud.insert(db, LegacyProgressRow(progress_key=key, checked_ids=json.dumps(ids)))


def _result(case_id, status, kind="human", approved=False):
    return CaseResult(
        progress_key=KEY, case_id=case_id, status=status, source="test",
        actor={"kind": kind}, review_approved=approved,
    )


# --- The fold -----------------------------------------------------------------


def test_a_pass_checks_and_anything_else_unchecks():
    results = [
        _result("happy_path:0", "passed"),
        _result("happy_path:1", "passed"),
        _result("happy_path:1", "cleared"),
        _result("edge_cases:0", "passed"),
        _result("edge_cases:0", "failed"),
    ]
    assert fold([], results) == ["happy_path:0"]


def test_legacy_marks_stand_until_a_result_changes_them():
    assert fold(["happy_path:0", "edge_cases:0"], [_result("edge_cases:0", "cleared")]) == [
        "happy_path:0"
    ]


def test_an_agents_unapproved_result_neither_ticks_nor_unticks():
    """An agent's pass is a proposal until a person approves it. It must not
    tick a box — and its failure must not untick one a person ticked."""
    results = [
        _result("happy_path:0", "passed"),
        _result("happy_path:0", "failed", kind="agent"),
        _result("happy_path:1", "passed", kind="agent"),
    ]
    assert fold([], results) == ["happy_path:0"]
    approved = [_result("happy_path:1", "passed", kind="agent", approved=True)]
    assert fold([], approved) == ["happy_path:1"]


# --- The lost update ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tick_and_a_runner_mark_landing_together_both_survive(db, api):
    """The lost update. The browser last polled before the runner marked
    happy_path:1; under the whole-set save its next toggle would have written
    {happy_path:0} and erased the runner's mark. Per-case, both stay."""
    plan = await _save_plan(db)
    browser_view = await test_plan_progress_repository.get_progress(db, progress_key=KEY)
    assert browser_view is None  # what the browser saw: nothing checked yet

    # The runner marks a case through the whole-set PUT, as mark-passed.sh does.
    r = await api.put(f"/test-plan-progress/{KEY}", json={"checked_ids": ["happy_path:1"]})
    assert r.status_code == 200

    # The browser, still holding its stale view, ticks a different case.
    r = await api.post(f"/plans/{plan.id}/results", json={"case_id": "happy_path:0", "status": "passed"})
    assert r.status_code == 200
    assert r.json()["checked_ids"] == ["happy_path:0", "happy_path:1"]


@pytest.mark.asyncio
async def test_unticking_one_case_leaves_every_other_mark_alone(db, api):
    plan = await _save_plan(db)
    await _legacy(db, KEY, ["happy_path:0", "edge_cases:0"])
    r = await api.post(f"/plans/{plan.id}/results", json={"case_id": "edge_cases:0", "status": "cleared"})
    assert r.json()["checked_ids"] == ["happy_path:0"]
    r = await api.get(f"/test-plan-progress/{KEY}")
    assert r.json()["checked_ids"] == ["happy_path:0"]


# --- Who passed it ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tick_names_the_configured_person(db, api, monkeypatch):
    monkeypatch.setattr(settings, "qa_actor_email", "tester@example.com")
    plan = await _save_plan(db)
    r = await api.post(f"/plans/{plan.id}/results", json={"case_id": "happy_path:0", "status": "passed"})
    assert r.json()["actor_named"] is True
    row = await db["case_results"].find_one({"case_id": "happy_path:0"})
    user = await user_repository.get_or_create_by_email(db, email="tester@example.com")
    assert row["actor"] == {"kind": "human", "user_id": user.id, "email": "tester@example.com", "name": None}
    assert row["plan_id"] == plan.id
    assert row["source"] == "ui"


@pytest.mark.asyncio
async def test_without_a_configured_person_a_tick_is_human_but_unnamed(db, api):
    """Never the Jira service account's address, which belongs to a robot."""
    plan = await _save_plan(db)
    r = await api.post(f"/plans/{plan.id}/results", json={"case_id": "happy_path:0", "status": "passed"})
    assert r.json()["actor_named"] is False
    row = await db["case_results"].find_one({"case_id": "happy_path:0"})
    assert row["actor"]["kind"] == "human"
    assert row["actor"]["email"] is None


@pytest.mark.asyncio
async def test_old_marks_keep_showing_and_are_attributed_to_no_one(db, api):
    """Nothing recorded who made a mark before results existed. Calling them a
    person's would make an unreviewed runner pass look reviewed."""
    plan = await _save_plan(db)
    await _legacy(db, KEY, ["happy_path:0", "regression_checklist:0"])
    r = await api.get(f"/test-plan-progress/{KEY}")
    assert r.json()["checked_ids"] == ["happy_path:0", "regression_checklist:0"]

    r = await api.get(f"/plans/{plan.id}/results")
    by_id = {x["case_id"]: x for x in r.json()["results"]}
    assert by_id["happy_path:0"]["actor"]["kind"] == "legacy_unknown"
    assert by_id["happy_path:0"]["source"] == "legacy"
    assert set(by_id) == {"happy_path:0", "regression_checklist:0"}


@pytest.mark.asyncio
async def test_the_whole_set_put_is_recorded_as_unknown_not_as_a_person(db, api):
    """The PUT is how a script writes; the server cannot tell it from a person."""
    await _save_plan(db)
    await api.put(f"/test-plan-progress/{KEY}", json={"checked_ids": ["happy_path:0"]})
    row = await db["case_results"].find_one({"case_id": "happy_path:0"})
    assert row["actor"]["kind"] == "legacy_unknown"
    assert row["source"] == "put"


@pytest.mark.asyncio
async def test_the_whole_set_put_still_clears_with_an_empty_list(db, api):
    await _save_plan(db)
    await _legacy(db, KEY, ["happy_path:0", "happy_path:1"])
    r = await api.put(f"/test-plan-progress/{KEY}", json={"checked_ids": []})
    assert r.json()["checked_ids"] == []
    statuses = [d["status"] async for d in db["case_results"].find({})]
    assert statuses == ["cleared", "cleared"]


@pytest.mark.asyncio
async def test_resending_a_mark_writes_no_second_result(db, api):
    plan = await _save_plan(db)
    for _ in range(2):
        r = await api.post(f"/plans/{plan.id}/results", json={"case_id": "happy_path:0", "status": "passed"})
    assert r.json()["changed"] is False
    assert await db["case_results"].count_documents({}) == 1


# --- Refusals -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_id_that_names_no_case_is_refused_and_writes_nothing(db, api):
    plan = await _save_plan(db)
    for bad in ("happy_path:9", "tc-happy_path-0"):
        r = await api.post(f"/plans/{plan.id}/results", json={"case_id": bad, "status": "passed"})
        assert r.status_code == 422
        assert r.json()["detail"]["unknown_ids"] == [bad]
    assert await db["case_results"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_a_status_other_than_passed_or_cleared_is_refused(db, api):
    """The checklist only ticks and unticks. Failures and Not Run arrive with
    evidence and a reason through the runner's own path, not a checkbox."""
    plan = await _save_plan(db)
    r = await api.post(f"/plans/{plan.id}/results", json={"case_id": "happy_path:0", "status": "failed"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_an_unknown_plan_is_a_404(db, api):
    r = await api.post("/plans/999/results", json={"case_id": "happy_path:0", "status": "passed"})
    assert r.status_code == 404


# --- Readers that used to read the stored row ---------------------------------


@pytest.mark.asyncio
async def test_progress_with_only_results_is_found_as_another_shape(db):
    """After a regeneration the UI asks which other shapes hold marks. A shape
    whose marks were all made after this change has no legacy row at all."""
    await test_plan_progress_repository.record_marks(
        db, progress_key="SK-1:1-0-0-0", plan_id=None, add=["happy_path:0"],
        actor={"kind": "human"}, source="ui",
    )
    views = await test_plan_progress_repository.find_for_same_tickets(db, progress_key=KEY)
    assert [v.progress_key for v in views] == ["SK-1:1-0-0-0"]
    assert json.loads(views[0].checked_ids) == ["happy_path:0"]


@pytest.mark.asyncio
async def test_a_carry_over_is_recorded_as_the_testers_own_passes(db, monkeypatch):
    monkeypatch.setattr(settings, "qa_actor_email", "tester@example.com")
    plan = await _save_plan(db)
    applied = await mark_carryover.apply(db, plan_id=plan.id, ids=["happy_path:1"])
    assert applied["added"] == ["happy_path:1"]
    row = await db["case_results"].find_one({"case_id": "happy_path:1"})
    assert row["source"] == "carryover"
    assert row["actor"]["email"] == "tester@example.com"
