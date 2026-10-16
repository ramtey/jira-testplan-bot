"""`post_state` on /runs/by-ticket: what the plan view's Post control reads.

The one property that matters most: "could not look" must never come back
looking like "not live", or the view offers to post a plan that is already
on the ticket.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src.app import runs_routes
from src.app.main import app

from .conftest import noop_get_db

client = TestClient(app)


def _get(*, live=(), last=None, live_error=None):
    with patch.object(runs_routes, "get_db", noop_get_db), patch.object(
        runs_routes.plan_repository, "list_runs_with_plans_by_ticket", AsyncMock(return_value=[])
    ), patch.object(
        runs_routes.jira_ticket_repository, "get_auto_bug_analysis_dispatched_at", AsyncMock(return_value=None)
    ), patch.object(
        runs_routes.plan_repository,
        "list_live_plans_for_ticket",
        AsyncMock(return_value=list(live), side_effect=live_error),
    ), patch.object(
        runs_routes.auto_post_repository, "get_last", AsyncMock(return_value=last)
    ):
        return client.get("/runs/by-ticket/sk-1")


def test_reports_the_live_own_plan_not_a_batch_one():
    live = [
        (SimpleNamespace(ticket_keys=["SK-1", "SK-2"]), SimpleNamespace(id=3, version=1, posted_at=None)),
        (
            SimpleNamespace(ticket_keys=["SK-1"]),
            SimpleNamespace(id=7, version=2, posted_at=datetime(2026, 10, 16, 10, tzinfo=timezone.utc)),
        ),
    ]
    body = _get(live=live).json()
    assert body["post_state"]["live_own_plan"] == {
        "plan_id": 7, "version": 2, "posted_at": "2026-10-16T10:00:00+00:00",
    }


def test_nothing_live_is_null_not_missing():
    body = _get().json()
    assert body["post_state"]["live_own_plan"] is None
    assert body["post_state"]["auto_post_status"] == runs_routes.settings.auto_post_status


def test_carries_the_last_auto_post_outcome():
    last = {"action": "skipped", "reason": "empty_plan", "detail": None, "plan_id": 7, "at": None}
    assert _get(last=last).json()["post_state"]["last_auto_post"] == last


def test_a_failed_lookup_is_unknown_and_history_still_loads():
    response = _get(live_error=RuntimeError("db down"))
    assert response.status_code == 200
    assert response.json()["post_state"] is None
    assert response.json()["runs"] == []
