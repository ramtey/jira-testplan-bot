"""Entry-point parity tests for services/plan_service.

The regression these guard against: before the service existed, the full
pipeline (deliverable classifier → generation → four critics → AC
coverage → run persistence) lived inline in the ``/generate-test-plan``
route handler, and every non-browser caller — the MCP server, the CLI —
hand-rolled its own payload assembly and called
``llm_client.generate_test_plan`` directly. Those plans silently had no
critics, no AC coverage and no persisted run.

So the assertions here are deliberately about *which pipeline ran*, not
about plan content: a key-only generate must produce the same
critic-badged, coverage-scored response the UI path produces, from the
same server-assembled context.
"""
import json
import pathlib
import re
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.app.main import app
from src.app.models import (
    Attachment,
    BounceEvent,
    ChildIssue,
    Commit,
    DescriptionAnalysis,
    DevelopmentInfo,
    GenerateTestPlanRequest,
    JiraComment,
    JiraIssue,
    LinkedIssue,
    LinkedIssues,
    ParentIssue,
    PullRequest,
    TestPlan,
)
from src.app.services import plan_service

client = TestClient(app)


def _issue(**overrides) -> JiraIssue:
    """A ticket enriched on every axis serialize_issue reads.

    Every field is populated precisely so a dropped one shows up as a
    missing key rather than as a None that looks plausible.
    """
    defaults = dict(
        key="SK-1",
        summary="Show assessed value in the results modal",
        description=(
            "Acceptance Criteria:\n"
            "1. Assessed value renders in the modal\n"
            "2. Loan amount renders for the seller net sheet\n"
        ),
        description_analysis=DescriptionAnalysis(
            has_description=True, gaps=[], char_count=120, word_count=20
        ),
        labels=["mobile"],
        issue_type="Story",
        assignee="Dev Person",
        assignee_account_id="acct-dev",
        assignee_history=["QA Person", "Dev Person"],
        assignee_history_account_ids=["acct-qa", "acct-dev"],
        development_info=DevelopmentInfo(
            commits=[Commit(message="SK-1 add assessed value", author="dev")],
            # A merged PR, because that is the only state that grounds a
            # full plan. These tests are about which pipeline stages run;
            # a ticket with no PR at all now short-circuits to the
            # no-implementation result before any stage does (see
            # tests/test_source_grounding.py), which would make every
            # assertion below vacuous.
            pull_requests=[
                PullRequest(
                    title="Show assessed value in the results modal",
                    status="MERGED",
                    url="https://github.com/acme/agent-calculator/pull/1200",
                    source_branch="feat/SK-1",
                    repository="acme/agent-calculator",
                    number=1200,
                    head_sha="a" * 40,
                    merged_at="2026-09-01T10:00:00Z",
                )
            ],
            branches=["feat/SK-1"],
        ),
        attachments=[
            Attachment(
                filename="mock.png",
                mime_type="image/png",
                size=1024,
                url="https://jira.example.com/attach/mock.png",
            )
        ],
        comments=[
            JiraComment(
                author="QA Person",
                body="Please also check the seller side.",
                created="2026-09-01T10:00:00.000+0000",
            )
        ],
        parent=ParentIssue(
            key="SK-0",
            summary="Property tax results",
            description="Parent epic body",
            issue_type="Epic",
            labels=[],
        ),
        children=[
            ChildIssue(
                key="SK-2",
                summary="Seller net sheet wiring",
                description="child body",
                issue_type="Sub-task",
            )
        ],
        linked_issues=LinkedIssues(
            blocks=[
                LinkedIssue(
                    key="SK-9",
                    summary="Downstream consumer",
                    description=None,
                    issue_type="Story",
                    link_type="blocks",
                )
            ]
        ),
        status="Ready to Test",
        status_category="indeterminate",
        bounce_history=[
            BounceEvent(
                from_status="Ready for UAT",
                to_status="In Progress",
                timestamp="2026-08-01T10:00:00.000+0000",
                author="QA Person",
                reason="Loan amount showed on the buyer side.",
            )
        ],
        story_points=3.0,
    )
    defaults.update(overrides)
    return JiraIssue(**defaults)


def _plan() -> TestPlan:
    return TestPlan(
        happy_path=[
            {
                "title": "Assessed value renders",
                "steps": ["Open the modal"],
                "expected": "Assessed value is shown",
                "priority": "critical",
                "covers_acs": ["AC1"],
            }
        ],
        edge_cases=[],
        regression_checklist=[],
    )


def _stub_llm(captured: dict):
    """An LLM stub that records the kwargs generation was called with.

    Critic methods return empty verdicts so every critic pass is exercised
    end-to-end without asserting on their content.
    """

    class _Stub:
        async def generate_test_plan(self, **kwargs):
            captured.update(kwargs)
            return _plan()

        async def classify_deliverable(self, **kwargs):
            captured["classify_deliverable_called"] = True
            return None

        async def verify_case_grounding(self, *a, **k):
            captured["verify_case_grounding_called"] = True
            return []

        async def verify_code_grounding(self, *a, **k):
            captured["verify_code_grounding_called"] = True
            return []

        async def verify_fix_scope(self, *a, **k):
            captured["verify_fix_scope_called"] = True
            return []

        async def verify_surface_alignment(self, *a, **k):
            captured["verify_surface_alignment_called"] = True
            return []

    return _Stub()


# ---------------------------------------------------------------------------
# Server-side context assembly
# ---------------------------------------------------------------------------


def test_prompt_payload_carries_every_enrichment_axis():
    """A key-only generate must see the same context the browser posts back.

    Each assertion stands for one enrichment the LLM prompt branches on;
    dropping any one silently degrades the plan rather than erroring.
    """
    payload = plan_service.prompt_payload(plan_service.serialize_issue(_issue()))

    assert payload["ticket_key"] == "SK-1"
    assert payload["issue_type"] == "Story"
    assert payload["development_info"]["branches"] == ["feat/SK-1"]
    assert payload["comments"][0]["author"] == "QA Person"
    assert payload["parent_info"]["key"] == "SK-0"
    assert payload["child_info"][0]["key"] == "SK-2"
    assert payload["linked_info"]["blocks"][0]["key"] == "SK-9"
    assert payload["bounce_history"][0]["to_status"] == "In Progress"
    assert payload["image_urls"] == ["https://jira.example.com/attach/mock.png"]


def test_prompt_payload_is_a_valid_generate_request():
    """The payload feeds GenerateTestPlanRequest directly, so its shape is
    the contract — not something a caller reshapes on the way in."""
    payload = plan_service.prompt_payload(plan_service.serialize_issue(_issue()))
    request = GenerateTestPlanRequest(**payload)
    assert request.ticket_key == "SK-1"
    assert request.bounce_history[0]["reason"].startswith("Loan amount")


def test_prompt_payload_matches_the_frontend_builder_field_for_field():
    """Guard against drift between the server assembler and the browser's
    ``buildTicketPayload``. If the two disagree, a plan generated by key
    stops matching the plan generated from the UI for the same ticket —
    exactly the divergence this service exists to remove."""
    js = pathlib.Path("frontend/src/hooks/useTestPlan.js").read_text()
    body = js[js.index("function buildTicketPayload") : js.index("\n}", js.index("function buildTicketPayload"))]
    frontend_keys = set(re.findall(r"^\s{4}(\w+):", body, re.MULTILINE))

    server_keys = set(plan_service.prompt_payload(plan_service.serialize_issue(_issue())))
    assert server_keys == frontend_keys


def test_prompt_payload_leaves_absent_enrichment_as_none():
    """A bare ticket must produce None, not empty containers — the prompt
    builders branch on falsiness and empty lists would render empty
    sections into the prompt."""
    bare = _issue(
        development_info=None,
        attachments=None,
        comments=None,
        parent=None,
        children=None,
        linked_issues=None,
        bounce_history=None,
    )
    payload = plan_service.prompt_payload(plan_service.serialize_issue(bare))
    for field in (
        "development_info",
        "image_urls",
        "comments",
        "parent_info",
        "child_info",
        "linked_info",
        "bounce_history",
    ):
        assert payload[field] is None, field


# ---------------------------------------------------------------------------
# The key-only entry point runs the whole pipeline
# ---------------------------------------------------------------------------


# Every pipeline stage the UI path runs, in the order plan_service runs them.
# Asserted at this level rather than on the LLM stub because each stage has
# its own feature gate (surface_classifier_enabled, github_token, merged-PR
# presence) — the claim under test is "the key-only door runs the same
# pipeline", not "every gate happened to be open".
PIPELINE_STAGES = (
    "classify_deliverable",
    "run_grounding_critic",
    "run_code_grounding_critic",
    "run_fix_scope_critic",
    "run_surface_mismatch_critic",
    "compute_ac_coverage",
)


@pytest.mark.asyncio
async def test_generate_for_ticket_runs_the_whole_pipeline():
    """The MCP/CLI regression, pinned: a key-only generate must run the
    classifier, all four critics and AC coverage — not the bare
    ``llm.generate_test_plan`` call those callers used to make."""
    captured: dict = {}
    calls: list[str] = []

    def _recorder(name, result=None):
        async def _async_stage(*a, **k):
            calls.append(name)
            return result

        return _async_stage

    with (
        patch.object(
            plan_service.JiraClient, "get_issue", AsyncMock(return_value=_issue())
        ),
        patch.object(
            plan_service.JiraClient,
            "download_image_as_base64",
            AsyncMock(return_value=None),
        ),
        patch.object(
            plan_service, "get_llm_client", return_value=_stub_llm(captured)
        ),
        patch.object(plan_service, "classify_deliverable", _recorder("classify_deliverable")),
        patch.object(plan_service, "run_grounding_critic", _recorder("run_grounding_critic")),
        patch.object(
            plan_service, "run_code_grounding_critic", _recorder("run_code_grounding_critic")
        ),
        patch.object(plan_service, "run_fix_scope_critic", _recorder("run_fix_scope_critic")),
        patch.object(
            plan_service,
            "run_surface_mismatch_critic",
            _recorder("run_surface_mismatch_critic"),
        ),
        patch.object(
            plan_service,
            "compute_ac_coverage",
            lambda *a, **k: calls.append("compute_ac_coverage") or {},
        ),
    ):
        response = await plan_service.generate_for_ticket("SK-1")

    assert calls == list(PIPELINE_STAGES)
    assert "ac_coverage" in response
    assert "grounding_warnings" in response
    # And the server-assembled context actually reached the prompt.
    assert captured["ticket_key"] == "SK-1"
    assert captured["bounce_history"][0]["to_status"] == "In Progress"
    assert captured["parent_info"]["key"] == "SK-0"
    assert captured["comments"][0]["author"] == "QA Person"


@pytest.mark.asyncio
async def test_generate_single_and_generate_for_ticket_run_the_same_stages():
    """The UI door and the key-only door must not diverge — that divergence
    is the whole reason this module exists."""
    calls: list[str] = []

    def _recorder(name):
        async def _async_stage(*a, **k):
            calls.append(name)

        return _async_stage

    patches = [
        patch.object(
            plan_service.JiraClient,
            "download_image_as_base64",
            AsyncMock(return_value=None),
        ),
        patch.object(plan_service, "get_llm_client", return_value=_stub_llm({})),
    ] + [patch.object(plan_service, stage, _recorder(stage)) for stage in PIPELINE_STAGES[:-1]]

    with patch.object(
        plan_service.JiraClient, "get_issue", AsyncMock(return_value=_issue())
    ):
        for ctx in patches:
            ctx.start()
        try:
            await plan_service.generate_for_ticket("SK-1")
            from_key = list(calls)
            calls.clear()
            payload = plan_service.prompt_payload(plan_service.serialize_issue(_issue()))
            await plan_service.generate_single(GenerateTestPlanRequest(**payload))
            from_payload = list(calls)
        finally:
            for ctx in patches:
                ctx.stop()

    assert from_key == from_payload == list(PIPELINE_STAGES[:-1])


@pytest.mark.asyncio
async def test_generate_for_ticket_rejects_non_testable_type():
    with patch.object(
        plan_service.JiraClient,
        "get_issue",
        AsyncMock(return_value=_issue(issue_type="Epic")),
    ):
        with pytest.raises(plan_service.NonTestableIssueError) as excinfo:
            await plan_service.generate_for_ticket("SK-1")
    assert "Epic" in str(excinfo.value)


@pytest.mark.asyncio
async def test_generate_for_tickets_routes_a_lone_key_to_the_single_pipeline():
    """One key is a single-ticket plan, not a degenerate multi one — the
    two produce differently shaped responses and callers rely on that."""
    captured: dict = {}
    with (
        patch.object(
            plan_service.JiraClient, "get_issue", AsyncMock(return_value=_issue())
        ),
        patch.object(
            plan_service.JiraClient,
            "download_image_as_base64",
            AsyncMock(return_value=None),
        ),
        patch.object(
            plan_service, "get_llm_client", return_value=_stub_llm(captured)
        ),
    ):
        response = await plan_service.generate_for_tickets(["SK-1"])

    assert response["ticket_key"] == "SK-1"
    assert "ticket_keys" not in response


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------


def test_post_tickets_plan_generates_from_a_key_alone():
    captured: dict = {}
    with (
        patch.object(
            plan_service.JiraClient, "get_issue", AsyncMock(return_value=_issue())
        ),
        patch.object(
            plan_service.JiraClient,
            "download_image_as_base64",
            AsyncMock(return_value=None),
        ),
        patch.object(
            plan_service, "get_llm_client", return_value=_stub_llm(captured)
        ),
    ):
        response = client.post("/tickets/sk-1/plan")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ticket_key"] == "SK-1"
    assert body["happy_path"][0]["title"] == "Assessed value renders"


def test_post_tickets_plan_rejects_non_testable_type_with_400():
    with patch.object(
        plan_service.JiraClient,
        "get_issue",
        AsyncMock(return_value=_issue(issue_type="Spike")),
    ):
        response = client.post("/tickets/SK-1/plan")
    assert response.status_code == 400
    assert "Spike" in response.json()["detail"]


def test_post_tickets_plan_maps_a_missing_ticket_to_404():
    from src.app.jira_client import JiraNotFoundError

    with patch.object(
        plan_service.JiraClient,
        "get_issue",
        AsyncMock(side_effect=JiraNotFoundError("SK-404 not found")),
    ):
        response = client.post("/tickets/SK-404/plan")
    assert response.status_code == 404


def test_post_tickets_plan_maps_llm_failure_to_503():
    from src.app.llm_client import LLMError

    class _FailingLLM:
        async def classify_deliverable(self, **kwargs):
            return None

        async def generate_test_plan(self, **kwargs):
            raise LLMError("Claude API request timed out")

    with (
        patch.object(
            plan_service.JiraClient, "get_issue", AsyncMock(return_value=_issue())
        ),
        patch.object(
            plan_service.JiraClient,
            "download_image_as_base64",
            AsyncMock(return_value=None),
        ),
        patch.object(plan_service, "get_llm_client", return_value=_FailingLLM()),
    ):
        response = client.post("/tickets/SK-1/plan")
    assert response.status_code == 503
    assert "timed out" in response.json()["detail"]


def test_post_tickets_plan_accepts_comma_separated_keys_as_a_multi_plan():
    """The multi-ticket door, so an automation caller doesn't have to know
    which of two endpoints to hit."""
    captured: dict = {}

    class _MultiStub:
        async def classify_deliverable(self, **kwargs):
            return None

        async def generate_multi_ticket_test_plan(self, **kwargs):
            captured.update(kwargs)
            return _plan()

        async def verify_case_grounding(self, *a, **k):
            return []

        async def verify_code_grounding(self, *a, **k):
            return []

        async def verify_fix_scope(self, *a, **k):
            return []

        async def verify_surface_alignment(self, *a, **k):
            return []

    async def _fake_get_issue(self, key):
        return _issue(key=key.upper())

    with (
        patch.object(plan_service.JiraClient, "get_issue", _fake_get_issue),
        patch.object(
            plan_service.JiraClient,
            "download_image_as_base64",
            AsyncMock(return_value=None),
        ),
        patch.object(plan_service, "get_llm_client", return_value=_MultiStub()),
    ):
        response = client.post("/tickets/SK-1,SK-2/plan")

    assert response.status_code == 200, response.text
    assert response.json()["ticket_keys"] == ["SK-1", "SK-2"]
    assert [t["ticket_key"] for t in captured["tickets"]] == ["SK-1", "SK-2"]


def test_generate_test_plan_route_still_accepts_a_client_assembled_payload():
    """The browser path is unchanged by the extraction — it keeps posting
    the context it already fetched."""
    captured: dict = {}
    payload = plan_service.prompt_payload(plan_service.serialize_issue(_issue()))

    with (
        patch.object(
            plan_service.JiraClient,
            "download_image_as_base64",
            AsyncMock(return_value=None),
        ),
        patch.object(
            plan_service, "get_llm_client", return_value=_stub_llm(captured)
        ),
    ):
        response = client.post("/generate-test-plan", json=payload)

    assert response.status_code == 200, response.text
    assert response.json()["ticket_key"] == "SK-1"
    assert json.dumps(response.json())  # response is JSON-serializable as stored
