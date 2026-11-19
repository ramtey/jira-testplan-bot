"""The Pass-to-UAT walkthrough gate keys on where the plan's cases run.

SK-3014 (plan 734 v1) was `uat_complexity: "high"` with every case an API or
terminal check: lock columns, a Postgres RPC and API logic, no UI. The video
walkthrough builder found nothing to film, and the gate still returned 409
until the tester sent `override_missing_walkthrough`. These tests run the real
`fetch_readiness` and `derive_readiness` behind the real route; only the
database rows and Jira are stubbed.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.app import uat_readiness, uat_surface, video_walkthrough, workflow_routes
from src.app.config import settings
from src.app.main import app
from src.app.repositories import walkthrough_repository

from .conftest import noop_get_db

client = TestClient(app)

FIXTURE = Path(__file__).parent / "fixtures" / "sk3014" / "plan734_surfaces.json"
PLAN_CREATED = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _enable_sk_workflow(monkeypatch):
    monkeypatch.setattr(settings, "workflow_project_prefixes", ["SK"])


def _sk3014_plan() -> dict:
    return json.loads(FIXTURE.read_text())


def _with_case(plan: dict, section: str, surface: str) -> dict:
    out = copy.deepcopy(plan)
    out[section] = list(out.get(section) or []) + [
        {"title": f"A {surface} case", "priority": "high", "surface": surface,
         "steps": ["step 1", "step 2"]}
    ]
    return out


def _ui_plan() -> dict:
    return {
        "ticket_key": "SK-1",
        "uat_complexity": "high",
        "happy_path": [
            {"title": "Save the form", "surface": "web_ui", "steps": ["open", "save"]},
        ],
        "edge_cases": [
            {"title": "Save on the phone", "surface": "mobile", "steps": ["open", "save"]},
        ],
    }


def _jira_stub(attachments=None, attachments_error=None):
    jira = MagicMock()
    jira.list_transitions = AsyncMock(return_value=[{"id": "31", "to": {"name": "Ready for UAT"}}])
    jira.get_my_account_id = AsyncMock(return_value="bot-1")
    jira.get_prior_assignee_account_id = AsyncMock(return_value=("dev-1", "Dev One"))
    jira.get_top_pr_contributor_account_id = AsyncMock(return_value=(None, None))
    jira.transition_issue = AsyncMock(return_value=None)
    jira.assign_issue = AsyncMock(return_value=None)
    jira.post_qa_pass_comment = AsyncMock(return_value={"id": "c-1"})
    jira.get_sibling_subtasks_info = AsyncMock(return_value=None)
    jira.get_issue_status = AsyncMock(return_value="Ready to Test")
    jira.get_subtasks_of = AsyncMock(return_value=[])
    jira.get_attachments = AsyncMock(
        return_value=attachments or [], side_effect=attachments_error
    )
    return jira


def _pass_to_uat(plan: dict | None, *, payload: str = "{}", walkthrough=None, jira=None):
    """POST pass-to-uat with the latest plan and walkthrough row stubbed."""
    jira = jira or _jira_stub()
    stored = (
        SimpleNamespace(body=json.dumps(plan), created_at=PLAN_CREATED)
        if plan is not None else None
    )
    with patch("src.app.workflow_routes.JiraClient", return_value=jira), \
            patch.object(workflow_routes, "get_db", noop_get_db), \
            patch.object(uat_readiness.plan_repository, "find_latest_plan_for_ticket",
                         AsyncMock(return_value=stored)), \
            patch.object(uat_readiness.walkthrough_repository, "get_walkthrough",
                         AsyncMock(return_value=walkthrough)):
        response = client.post("/issue/SK-3014/workflow/pass-to-uat", data={"payload": payload})
    return response, jira


# --- the signal ------------------------------------------------------------


def test_sk3014_plan_is_backend_only_and_has_no_video_chapters():
    plan = _sk3014_plan()
    assert plan["uat_complexity"] == "high"
    assert uat_surface.classify(plan) == uat_surface.BACKEND
    # The two readers of `surface` agree: nothing here can be filmed.
    assert video_walkthrough.walkthrough_from_plan_body(plan)["chapters"] == []


@pytest.mark.parametrize(
    "extra_surface, expected",
    [
        ("web_ui", uat_surface.MIXED),
        ("mobile", uat_surface.MIXED),
        ("manual_only", uat_surface.MANUAL),
        ("", uat_surface.UNKNOWN),
    ],
)
def test_one_non_backend_case_changes_the_surface(extra_surface, expected):
    # Added to edge_cases, not happy_path: a UI check outside the happy path
    # still needs a walkthrough, which is why the gate does not use the video
    # builder (it only reads happy_path).
    plan = _with_case(_sk3014_plan(), "edge_cases", extra_surface)
    assert uat_surface.classify(plan) == expected


def test_unit_covered_ui_case_does_not_make_a_plan_ui():
    plan = _sk3014_plan()
    plan["edge_cases"].append(
        {"title": "Covered", "surface": "web_ui", "covered_by_unit_test": True, "steps": ["x"]}
    )
    assert uat_surface.classify(plan) == uat_surface.BACKEND


def test_ui_only_and_empty_plans():
    assert uat_surface.classify(_ui_plan()) == uat_surface.UI
    assert uat_surface.classify({"happy_path": []}) == uat_surface.UNKNOWN
    assert uat_surface.classify("not json") == uat_surface.UNKNOWN


# --- the gate, end to end --------------------------------------------------


def test_high_complexity_backend_only_plan_passes_without_override():
    response, jira = _pass_to_uat(_sk3014_plan())
    assert response.status_code == 200, response.text
    jira.transition_issue.assert_awaited_once()
    # Decided from the plan alone: Jira is not asked for attachments.
    jira.get_attachments.assert_not_awaited()


def test_high_complexity_ui_plan_still_gets_409():
    response, jira = _pass_to_uat(_ui_plan())
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error_code"] == "walkthrough_required"
    assert detail["uat_surface"] == "ui"
    assert detail["walkthrough_reason"] == walkthrough_repository.REASON_UI
    jira.transition_issue.assert_not_awaited()


def test_mixed_plan_still_gets_409():
    response, jira = _pass_to_uat(_with_case(_sk3014_plan(), "edge_cases", "web_ui"))
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["uat_surface"] == "mixed"
    assert detail["walkthrough_reason"] == walkthrough_repository.REASON_UI
    jira.transition_issue.assert_not_awaited()
    # A text attachment on Jira does not stand in for a walkthrough of UI.
    jira.get_attachments.assert_not_awaited()


@pytest.mark.parametrize("plan_factory", [_ui_plan, lambda: _with_case(_sk3014_plan(), "happy_path", "web_ui")])
def test_override_still_passes_a_gated_plan(plan_factory):
    response, jira = _pass_to_uat(plan_factory(), payload='{"override_missing_walkthrough": true}')
    assert response.status_code == 200, response.text
    jira.transition_issue.assert_awaited_once()


def test_plan_without_surfaces_keeps_the_old_gate():
    plan = {"uat_complexity": "high", "happy_path": [{"title": "Old case", "steps": ["a"]}]}
    response, _ = _pass_to_uat(plan)
    assert response.status_code == 409
    assert response.json()["detail"]["walkthrough_reason"] == walkthrough_repository.REASON_UNKNOWN


# --- written evidence for plans with manual checks -------------------------


def _manual_plan() -> dict:
    return _with_case(_sk3014_plan(), "integration_tests", "manual_only")


def test_manual_plan_with_text_attachment_after_the_plan_passes():
    jira = _jira_stub(attachments=[
        {"filename": "test-plan-SK-3014.md", "created": "2026-11-11T10:00:00.000-0800"},
        {"filename": "sk3014-results.md", "created": "2026-11-12T09:30:00.000-0800",
         "mimeType": "text/markdown"},
        {"filename": "sk3014-evidence.zip", "created": "2026-11-12T09:31:00.000-0800",
         "mimeType": "application/zip"},
    ])
    response, jira = _pass_to_uat(_manual_plan(), jira=jira)
    assert response.status_code == 200, response.text
    jira.get_attachments.assert_awaited_once_with("SK-3014")


def test_manual_plan_ignores_the_bots_plan_file_and_older_attachments():
    jira = _jira_stub(attachments=[
        {"filename": "test-plan-SK-3014.md", "created": "2026-11-11T10:00:00.000-0800"},
        {"filename": "spec.md", "created": "2026-11-01T10:00:00.000-0800"},
    ])
    response, _ = _pass_to_uat(_manual_plan(), jira=jira)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["walkthrough_reason"] == walkthrough_repository.REASON_MANUAL
    assert ".zip" in detail["message"]


def test_manual_plan_when_jira_is_unreachable_says_unchecked_not_missing():
    jira = _jira_stub(attachments_error=RuntimeError("Jira down"))
    response, _ = _pass_to_uat(_manual_plan(), jira=jira)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["walkthrough_reason"] == walkthrough_repository.REASON_UNCHECKED
    assert "could not be asked" in detail["message"]


def test_manual_plan_with_notes_on_the_card_passes_without_asking_jira():
    row = SimpleNamespace(loom_url=None, screenshots=None, notes="Ran the pgTAP suite: 14/14", updated_at=None)
    response, jira = _pass_to_uat(_manual_plan(), walkthrough=row)
    assert response.status_code == 200, response.text
    jira.get_attachments.assert_not_awaited()


# --- readiness response ----------------------------------------------------


@pytest.mark.parametrize(
    "complexity, surface, sources_row, evidence, needs, reason",
    [
        ("low", "ui", None, [], False, "not_high_complexity"),
        (None, None, None, [], False, "not_high_complexity"),
        ("high", "ui", "notes", [], False, "walkthrough_present"),
        ("high", "backend", None, [], False, "backend_only"),
        ("high", "manual", None, [{"filename": "r.md"}], False, "evidence_attached"),
        ("high", "manual", None, [], True, "manual_checks_need_evidence"),
        ("high", "manual", None, None, True, "attachments_unchecked"),
        ("high", "ui", None, [{"filename": "r.md"}], True, "ui_needs_walkthrough"),
        ("high", "mixed", None, [], True, "ui_needs_walkthrough"),
        ("high", "unknown", None, [], True, "surface_unknown"),
        ("high", None, None, [], True, "surface_unknown"),
    ],
)
def test_derive_readiness_reasons(complexity, surface, sources_row, evidence, needs, reason):
    wt = {"loom_url": None, "screenshots": [], "notes": "seen it" if sources_row == "notes" else None}
    out = walkthrough_repository.derive_readiness(
        wt, complexity, uat_surface=surface, evidence_attachments=evidence
    )
    assert out["needs_walkthrough"] is needs
    assert out["walkthrough_reason"] == reason


def test_readiness_response_names_surface_and_reason():
    import asyncio

    stored = SimpleNamespace(body=json.dumps(_sk3014_plan()), created_at=PLAN_CREATED)
    with patch.object(uat_readiness.plan_repository, "find_latest_plan_for_ticket",
                      AsyncMock(return_value=stored)), \
            patch.object(uat_readiness.walkthrough_repository, "get_walkthrough",
                         AsyncMock(return_value=None)):
        data = asyncio.run(uat_readiness.fetch_readiness(None, ticket_key="sk-3014"))
    assert data["uat_complexity"] == "high"
    assert data["uat_surface"] == "backend"
    assert data["needs_walkthrough"] is False
    assert data["walkthrough_reason"] == "backend_only"
    assert data["evidence_attachments"] == []
