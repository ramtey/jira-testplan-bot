"""The UAT runner reads its plan from the store, not from the Jira comment.

The comment is a rendering. A large plan's comment is only an index, and the
runner's pairing of cases with ids by reading the comment is what marked the
wrong case on SK-2325. ``GET /tickets/{key}/live-plan`` hands it each case
under the id ``mark-passed.sh`` will validate against, so the read and the
write share one definition.

These tests pin that the ids are the ``case_index`` ids, that only the plan
live in Jira is returned, and that a store failure is a 503 — never an empty
list the runner would report as "this ticket has no plan".
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.app.repositories import plan_repository
from src.app.services.progress_key import case_index, cases_by_id


def _plan_body() -> str:
    return json.dumps(
        {
            "happy_path": [
                {"title": "Opens the form", "steps": ["Tap New"]},
                {"title": "Covered already", "covered_by_unit_test": True},
                {"title": "Saves the form", "steps": ["Tap Save"]},
            ],
            "edge_cases": [{"title": "Rejects a bad token"}],
            "integration_tests": [],
            "regression_checklist": ["Login still works"],
        }
    )


def test_cases_carry_exactly_the_case_index_ids_in_the_same_order():
    body = _plan_body()
    assert [c["id"] for c in cases_by_id(body)] == list(case_index(body))


def test_a_case_after_a_covered_one_keeps_its_full_content_under_its_uncovered_index():
    by_id = {c["id"]: c for c in cases_by_id(_plan_body())}
    assert by_id["happy_path:1"]["case"]["steps"] == ["Tap Save"]
    assert by_id["covered_by_unit_test:0"]["case"]["title"] == "Covered already"
    assert by_id["regression_checklist:0"]["case"] == "Login still works"


# --- repository ------------------------------------------------------------


class _Recorder:
    def __init__(self, docs):
        self.docs = docs
        self.queries: list[tuple[str, dict]] = []

    def __getitem__(self, name):
        rec = self

        class _Coll:
            def find(self, filter_=None, projection=None, **kwargs):
                rec.queries.append((name, filter_ or {}))
                docs = rec.docs.get(name, [])

                class _Cur:
                    def sort(self, *a, **k):
                        return self

                    def limit(self, *a, **k):
                        return self

                    async def to_list(self, length=None):
                        return list(docs)

                return _Cur()

        return _Coll()


@pytest.mark.asyncio
async def test_only_plans_posted_to_jira_are_asked_for_and_nothing_caps_the_runs():
    db = _Recorder(
        {
            "runs": [
                {
                    "_id": 1,
                    "ticket_keys": ["SK-2325"],
                    "status": "ok",
                    "run_type": "test_plan",
                    "model": "claude-opus-5",
                    "llm_provider": "claude",
                    "user_id": 1,
                }
            ],
            "generated_plans": [
                {
                    "_id": 536,
                    "run_id": 1,
                    "format": "json",
                    "body": _plan_body(),
                    "case_count": 5,
                    "version": 3,
                    "jira_comment_id": "10001",
                }
            ],
        }
    )
    live = await plan_repository.list_live_plans_for_ticket(db, ticket_key="SK-2325")

    assert [p.id for _run, p in live] == [536]
    plan_filters = [f for name, f in db.queries if name == "generated_plans"]
    assert plan_filters == [{"run_id": {"$in": [1]}, "jira_comment_id": {"$ne": None}}]


# --- route -----------------------------------------------------------------


def _client(monkeypatch, fake):
    from fastapi.testclient import TestClient
    from src.app import main, runs_routes

    monkeypatch.setattr(runs_routes, "get_db", lambda: object())
    monkeypatch.setattr(
        runs_routes.plan_repository, "list_live_plans_for_ticket", fake
    )
    return TestClient(main.app)


def test_a_store_failure_is_503_not_an_empty_plan_list(monkeypatch):
    async def boom(_db, *, ticket_key):
        raise RuntimeError("mongo down")

    r = _client(monkeypatch, boom).get("/tickets/sk-2325/live-plan")
    assert r.status_code == 503


def test_a_ticket_with_nothing_posted_has_no_live_plans(monkeypatch):
    async def none(_db, *, ticket_key):
        return []

    r = _client(monkeypatch, none).get("/tickets/sk-2325/live-plan")
    assert r.status_code == 200
    assert r.json() == {"ticket_key": "SK-2325", "live_plans": []}


def test_the_live_plan_carries_its_key_and_every_case_by_id(monkeypatch):
    run = SimpleNamespace(ticket_keys=["SK-2325", "SK-2326"])
    plan = SimpleNamespace(
        id=536,
        version=3,
        body=_plan_body(),
        jira_comment_id="10001",
        posted_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )

    async def one(_db, *, ticket_key):
        assert ticket_key == "SK-2325"
        return [(run, plan)]

    body = _client(monkeypatch, one).get("/tickets/sk-2325/live-plan").json()
    [live] = body["live_plans"]
    assert live["plan_id"] == 536
    assert live["batch"] is True
    assert live["progress_key"].startswith("SK-2325+SK-2326:")
    assert [c["id"] for c in live["cases"]] == list(case_index(plan.body))
