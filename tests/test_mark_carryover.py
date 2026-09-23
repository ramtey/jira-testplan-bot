"""Carrying QA marks across a regeneration.

Why this file exists
--------------------
Progress is keyed by a fingerprint of the plan's section sizes, so a regenerated
plan whose shape changed gets a fresh key and every prior mark stops applying.
That is deliberate: ``progress_key`` exists because a stale check landing on a
*different* case is the worst thing this app can do. The consequence had no
remedy, though. SK-2325 regenerated from ``SK-2325:4-6-2-10`` (plan 532) to
``SK-2325:5-8-0-9-4`` (plan 536) and stranded sixteen marks, and the tester
re-mapped them by hand across two Jira comments in two browser tabs — some cases
had moved section (the zero/false-retention checks went from ``edge_cases`` to
``happy_path``), some had been reworded, two had no equivalent at all.

``/test-plan-progress/{key}/other-shapes`` could already *say* the marks existed
while refusing to move them, on the grounds that the judgement is the tester's.
These pin the middle ground: the server proposes, names what changed, and writes
only what it is handed back.

The failure to keep guarding against is a carry-over that lands somewhere
nothing renders — the invisible-progress family that cost SK-2642 and SK-2327 —
so the tests that matter most here are the ones about refusing an id.
"""

from __future__ import annotations

import json

import pytest
from mongomock_motor import AsyncMongoMockClient

from src.app.db import mongo as db_mongo
from src.app.db.models.plan import PlanFormat
from src.app.db.models.run import RunStatus, RunType
from src.app.repositories import (
    plan_repository,
    run_repository,
    test_plan_progress_repository,
    user_repository,
)
from src.app.services import mark_carryover
from src.app.services.progress_key import build_progress_key


@pytest.fixture
def db(monkeypatch):
    client = AsyncMongoMockClient()
    database = client["testplan_test"]
    monkeypatch.setattr(db_mongo, "_client", client)
    monkeypatch.setattr(db_mongo, "_db", database)
    monkeypatch.setattr(db_mongo, "_supports_transactions", False)
    return database


async def _save_plan(db, body: dict, *, ticket_key="SK-2325"):
    user = await user_repository.get_or_create_by_email(db, email="qa@example.com")
    run = await run_repository.create(
        db,
        user_id=user.id,
        run_type=RunType.test_plan,
        ticket_keys=[ticket_key],
        model="claude-opus-5",
        llm_provider="claude",
        status=RunStatus.ok,
    )
    plan = await plan_repository.save_with_cases(
        db, run_id=run.id, format=PlanFormat.json, body=json.dumps(body), cases=[]
    )
    return plan


# The SK-2325 pair, reduced to the four moves that made the hand-mapping hard.
OLD = {
    "happy_path": [{"title": "Saves a property"}],
    "edge_cases": [
        {"title": "Zero values are retained, not coerced to blank"},
        {"title": "Rejects a malformed token"},
        {"title": "A case the regenerated plan drops entirely"},
    ],
    "integration_tests": [],
    "regression_checklist": ["Login still works"],
}

NEW = {
    # moved section, and reworded
    "happy_path": [
        {"title": "Saves a property"},
        {"title": "Zero and false are retained on save"},
    ],
    "edge_cases": [{"title": "Rejects a malformed token"}],
    "integration_tests": [
        {"title": "Backend accepts the payload", "covered_by_unit_test": True}
    ],
    "regression_checklist": ["Login still works"],
}


async def _seed(db, *, checked: list[str]):
    old_plan = await _save_plan(db, OLD)
    await test_plan_progress_repository.upsert_progress(
        db,
        progress_key=build_progress_key(["SK-2325"], json.dumps(OLD)),
        checked_ids=checked,
    )
    new_plan = await _save_plan(db, NEW)
    return old_plan, new_plan


@pytest.mark.asyncio
async def test_an_unchanged_case_is_proposed_as_the_same_case(db):
    _old, new = await _seed(db, checked=["happy_path:0"])
    result = await mark_carryover.build(db, plan_id=new.id)
    mark = next(m for m in result["marks"] if m["from"] == "happy_path:0")
    assert mark["status"] == "same"
    assert mark["to"] == "happy_path:0"


@pytest.mark.asyncio
async def test_a_case_that_moved_section_is_still_matched(db):
    """The zero/false-retention move — `edge_cases:0` to `happy_path:1`. Matching
    on section would have lost it, and it is the move that cost the most hand
    work on SK-2325."""
    _old, new = await _seed(db, checked=["edge_cases:0"])
    result = await mark_carryover.build(db, plan_id=new.id)
    mark = next(m for m in result["marks"] if m["from"] == "edge_cases:0")
    assert mark["to"] == "happy_path:1"
    assert mark["status"] == "reworded"
    assert mark["from_title"] == "Zero values are retained, not coerced to blank"
    assert mark["to_title"] == "Zero and false are retained on save"


@pytest.mark.asyncio
async def test_a_case_the_new_plan_dropped_is_reported_not_guessed_at(db):
    """"You tested something that is gone" is information. Attaching the mark to
    the nearest surviving case instead would be the silent mismark this whole
    area exists to prevent."""
    _old, new = await _seed(db, checked=["edge_cases:2"])
    result = await mark_carryover.build(db, plan_id=new.id)
    mark = next(m for m in result["marks"] if m["from"] == "edge_cases:2")
    assert mark["status"] == "gone"
    assert mark["to"] is None


@pytest.mark.asyncio
async def test_two_old_marks_never_land_on_the_same_new_case(db):
    """Otherwise one tick would be reported twice and a second case would look
    untested while its match was consumed."""
    _old, new = await _seed(db, checked=["happy_path:0", "edge_cases:0", "edge_cases:1"])
    result = await mark_carryover.build(db, plan_id=new.id)
    targets = [m["to"] for m in result["marks"] if m["to"]]
    assert len(targets) == len(set(targets))


@pytest.mark.asyncio
async def test_a_covered_case_can_be_a_carry_over_target(db):
    """Covered cases are optional, not absent — a mark that lands on one is a
    tester saying they ran it live anyway, which is the whole reason they became
    addressable."""
    _old, new = await _seed(db, checked=["happy_path:0"])
    result = await mark_carryover.build(db, plan_id=new.id)
    applied = await mark_carryover.apply(
        db, plan_id=new.id, ids=["covered_by_unit_test:0"]
    )
    assert applied["checked_ids"] == ["covered_by_unit_test:0"]
    assert result["progress_key"] == "SK-2325:2-1-0-1-1"


@pytest.mark.asyncio
async def test_applying_writes_under_the_new_plans_own_key(db):
    _old, new = await _seed(db, checked=["happy_path:0"])
    applied = await mark_carryover.apply(db, plan_id=new.id, ids=["happy_path:0"])
    assert applied["progress_key"] == build_progress_key(["SK-2325"], json.dumps(NEW))
    row = await test_plan_progress_repository.get_progress(
        db, progress_key=applied["progress_key"]
    )
    assert json.loads(row.checked_ids) == ["happy_path:0"]


@pytest.mark.asyncio
async def test_applying_unions_rather_than_replaces(db):
    """A tester may have started on the new plan before carrying anything over.
    Replacing would silently undo their work."""
    _old, new = await _seed(db, checked=["happy_path:0"])
    key = build_progress_key(["SK-2325"], json.dumps(NEW))
    await test_plan_progress_repository.upsert_progress(
        db, progress_key=key, checked_ids=["edge_cases:0"]
    )
    applied = await mark_carryover.apply(db, plan_id=new.id, ids=["happy_path:0"])
    assert applied["checked_ids"] == ["edge_cases:0", "happy_path:0"]
    assert applied["added"] == ["happy_path:0"]


@pytest.mark.asyncio
async def test_an_id_that_names_no_case_is_refused(db):
    """The invisible-check failure, from the one operation most likely to cause
    it. `happy_path:9` would store cleanly and render nowhere."""
    _old, new = await _seed(db, checked=["happy_path:0"])
    result = await mark_carryover.apply(db, plan_id=new.id, ids=["happy_path:9"])
    assert result["error"] == "unknown_ids"
    assert result["unknown"] == ["happy_path:9"]
    row = await test_plan_progress_repository.get_progress(
        db, progress_key=build_progress_key(["SK-2325"], json.dumps(NEW))
    )
    assert row is None, "a refused carry-over must write nothing at all"


@pytest.mark.asyncio
async def test_the_dom_element_id_spelling_is_refused(db):
    """`tc-happy_path-0` is what the UI puts on a card for anchoring; what it
    stores is `happy_path:0`. Both spellings have reached progress rows before."""
    _old, new = await _seed(db, checked=["happy_path:0"])
    result = await mark_carryover.apply(db, plan_id=new.id, ids=["tc-happy_path-0"])
    assert result["error"] == "unknown_ids"


@pytest.mark.asyncio
async def test_a_ticket_with_no_earlier_marks_proposes_nothing(db):
    """Distinct from "nothing matched" — there is no source at all, and the
    dialog says so rather than showing an empty list that reads as a loss."""
    new = await _save_plan(db, NEW)
    result = await mark_carryover.build(db, plan_id=new.id)
    assert result["source"] is None
    assert result["marks"] == []


@pytest.mark.asyncio
async def test_progress_under_the_plans_own_key_is_not_offered_for_carry_over(db):
    """Otherwise every tested plan would offer to carry its own marks onto
    itself."""
    new = await _save_plan(db, NEW)
    await test_plan_progress_repository.upsert_progress(
        db,
        progress_key=build_progress_key(["SK-2325"], json.dumps(NEW)),
        checked_ids=["happy_path:0"],
    )
    result = await mark_carryover.build(db, plan_id=new.id)
    assert result["source"] is None


@pytest.mark.asyncio
async def test_marks_from_a_plan_that_is_gone_are_reported_by_id_only(db):
    """The row survives the plan it was recorded against. Without the plan there
    is no way to say which case an id named, and inventing one is worse than
    saying so."""
    new = await _save_plan(db, NEW)
    await test_plan_progress_repository.upsert_progress(
        db, progress_key="SK-2325:9-9-9-9", checked_ids=["happy_path:0"]
    )
    result = await mark_carryover.build(db, plan_id=new.id)
    assert result["source"]["resolvable"] is False
    assert result["marks"][0]["status"] == "unresolved"
    assert result["marks"][0]["to"] is None


@pytest.mark.asyncio
async def test_another_tickets_marks_are_never_offered(db):
    _old_other = await _save_plan(db, OLD, ticket_key="SK-2327")
    await test_plan_progress_repository.upsert_progress(
        db,
        progress_key=build_progress_key(["SK-2327"], json.dumps(OLD)),
        checked_ids=["happy_path:0"],
    )
    new = await _save_plan(db, NEW)
    result = await mark_carryover.build(db, plan_id=new.id)
    assert result["source"] is None


# ---------------------------------------------------------------------------
# The rule change itself.
#
# Adding the covered count to the fingerprint moved the key of every plan that
# has covered cases — including plans already being tested. SK-2325's plan 536
# went from `SK-2325:5-8-0-9` to `SK-2325:5-8-0-9-4` with sixteen marks recorded
# under the old string. Those marks name cases that all still exist, at the same
# indexes; only the key moved. Failing to trace them back would have turned a
# fix for unrecordable evidence into a loss of recorded evidence.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_marks_under_a_plans_own_pre_change_key_are_carried_back(db):
    """The one migration this change needs. Every id maps to itself, so the
    whole set comes back as exact matches and the tester confirms in one click."""
    from src.app.services.progress_key import legacy_progress_keys

    plan = await _save_plan(db, NEW)
    legacy = legacy_progress_keys(["SK-2325"], json.dumps(NEW))
    assert legacy == ["SK-2325:2-1-0-1"], "the pre-change key for this shape"
    await test_plan_progress_repository.upsert_progress(
        db, progress_key=legacy[0], checked_ids=["happy_path:0", "edge_cases:0"]
    )

    result = await mark_carryover.build(db, plan_id=plan.id)
    assert result["source"]["resolvable"] is True
    assert result["source"]["plan_id"] == plan.id
    assert {m["from"]: m["to"] for m in result["marks"]} == {
        "happy_path:0": "happy_path:0",
        "edge_cases:0": "edge_cases:0",
    }
    assert {m["status"] for m in result["marks"]} == {"same"}


@pytest.mark.asyncio
async def test_a_plan_with_no_covered_cases_has_no_pre_change_key(db):
    """Its key never moved, so claiming a legacy form would have it match rows
    belonging to a genuinely different plan shape."""
    from src.app.services.progress_key import legacy_progress_keys

    assert legacy_progress_keys(["SK-2325"], json.dumps(OLD)) == []
