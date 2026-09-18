"""The pipeline's two serialisation points, and the copy-only audit.

Generating a plan is mostly waiting: attachments download from Jira,
Slack links resolve, the deliverable classifier takes an LLM round-trip,
generation takes another, and then five critics take one each. Most of
that waiting used to be strictly sequential, including between steps
that never read each other's output. Two of those joins are now
concurrent, and the tests here are written so that reverting either one
*deadlocks* rather than merely gets slower — a timing assertion on a
test machine is a flake, but an event that nobody ever sets is a fact.

Also covered: the copy-only budget audit reaching ``source_provenance``,
because a budget check nothing can read afterwards is not a check. See
src/app/copy_only.py.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.app.models import GenerateTestPlanRequest, TestPlan  # noqa: E402
from src.app.services import plan_service  # noqa: E402


# A ticket with a merged PR — required, or `require_source_grounding`
# short-circuits to the no-implementation result before any stage runs.
def _request(**overrides) -> GenerateTestPlanRequest:
    payload = dict(
        ticket_key="SK-1",
        summary="Update the leave-group dialog copy",
        description="Acceptance Criteria:\n1. The dialog reads correctly\n",
        issue_type="Story",
        image_urls=["https://jira.example.com/attach/mock.png"],
        development_info={
            "pull_requests": [
                {
                    "title": "Update the leave-group dialog copy",
                    "status": "MERGED",
                    "url": "https://github.com/acme/app/pull/12",
                    "repository": "acme/app",
                    "number": 12,
                    "merged_at": "2026-09-01T10:00:00Z",
                }
            ],
            "commits": [],
            "branches": [],
        },
    )
    payload.update(overrides)
    return GenerateTestPlanRequest(**payload)


def _stub_llm(plan: TestPlan):
    class _Stub:
        async def generate_test_plan(self, **kwargs):
            return plan

        async def verify_case_grounding(self, *a, **k):
            return []

        async def verify_code_grounding(self, *a, **k):
            return []

        async def verify_fix_scope(self, *a, **k):
            return []

        async def verify_surface_alignment(self, *a, **k):
            return []

    return _Stub()


def _plan(cases=None, copy_only=None) -> TestPlan:
    return TestPlan(
        happy_path=cases if cases is not None else [
            {"title": "Dialog reads correctly", "steps": ["Open it"],
             "expected": "It reads correctly", "priority": "high"}
        ],
        edge_cases=[],
        regression_checklist=[],
        copy_only=copy_only,
    )


# ── Join 1: the three pre-generation legs ────────────────────────────────────


@pytest.mark.asyncio
async def test_attachments_slack_and_the_classifier_run_concurrently():
    """None of the three reads another's output, so running them in
    sequence only added their latencies together — and on a ticket with
    attachments and a Slack thread that was most of the wait before the
    first token of the plan.

    The image leg here refuses to finish until the Slack leg has started.
    Sequentially that is a deadlock; concurrently it is the normal case.
    """
    slack_started = asyncio.Event()

    async def _images(*_a, **_k):
        await asyncio.wait_for(slack_started.wait(), timeout=2)
        return None

    async def _slack(*_a, **_k):
        slack_started.set()
        return [], []

    with (
        patch.object(plan_service, "_download_images", _images),
        patch.object(plan_service, "resolve_slack_messages_in_text", _slack),
        patch.object(plan_service, "get_llm_client", return_value=_stub_llm(_plan())),
    ):
        response = await plan_service.generate_single(_request())

    assert response["ticket_key"] == "SK-1"


# ── Join 2: the regression critic against the case-critic chain ──────────────


@pytest.mark.asyncio
async def test_the_regression_critic_overlaps_the_case_critic_chain():
    """The regression pass reads and writes ``regression_checklist``; the
    four case critics read and write the three case sections and
    ``grounding_warnings``. Disjoint, so the regression pass has no reason
    to wait for them — and it is the slowest of the five.

    The last critic in the chain refuses to finish until the regression
    pass has started. Run in the old order the chain completes first and
    that event is never set, so this test deadlocks rather than passing
    slowly.
    """
    regression_started = asyncio.Event()
    chain_done = asyncio.Event()
    order: list[str] = []

    async def _regression(*_a, **_k):
        order.append("regression-start")
        regression_started.set()
        await asyncio.wait_for(chain_done.wait(), timeout=2)
        order.append("regression-end")
        return None

    async def _surface(*_a, **_k):
        await asyncio.wait_for(regression_started.wait(), timeout=2)
        order.append("chain-end")
        chain_done.set()
        return None

    with (
        patch.object(plan_service.JiraClient, "download_image_as_base64",
                     AsyncMock(return_value=None)),
        patch.object(plan_service, "run_regression_grounding_critic", _regression),
        patch.object(plan_service, "run_surface_mismatch_critic", _surface),
        patch.object(plan_service, "get_llm_client", return_value=_stub_llm(_plan())),
    ):
        await plan_service.generate_single(_request())

    assert order == ["regression-start", "chain-end", "regression-end"]


@pytest.mark.asyncio
async def test_a_regression_critic_gap_still_reaches_provenance():
    """Concurrency must not lose the critic's report of its own
    unavailability. "Checked and found nothing" and "never looked" are
    different claims, and only the first is evidence — the whole reason
    these passes return a sentence instead of None."""
    async def _regression(*_a, **_k):
        return "regression-grounding critic could not run (no GitHub token)"

    with (
        patch.object(plan_service.JiraClient, "download_image_as_base64",
                     AsyncMock(return_value=None)),
        patch.object(plan_service, "run_regression_grounding_critic", _regression),
        patch.object(plan_service, "get_llm_client", return_value=_stub_llm(_plan())),
    ):
        response = await plan_service.generate_single(_request())

    assert response["source_provenance"]["critics_unavailable"] == [
        "regression-grounding critic could not run (no GitHub token)"
    ]


@pytest.mark.asyncio
async def test_a_failing_chain_does_not_leave_the_regression_critic_running():
    """An orphaned critic task would carry on mutating a plan nobody will
    ship, and log its result into a request that already failed."""
    finished = asyncio.Event()

    async def _regression(*_a, **_k):
        try:
            await asyncio.sleep(0.05)
            return None
        finally:
            finished.set()

    async def _exploding_critic(*_a, **_k):
        raise RuntimeError("critic blew up")

    with (
        patch.object(plan_service.JiraClient, "download_image_as_base64",
                     AsyncMock(return_value=None)),
        patch.object(plan_service, "run_regression_grounding_critic", _regression),
        patch.object(plan_service, "run_grounding_critic", _exploding_critic),
        patch.object(plan_service, "get_llm_client", return_value=_stub_llm(_plan())),
    ):
        with pytest.raises(RuntimeError):
            await plan_service.generate_single(_request())

    assert finished.is_set(), "the regression critic was left running"


# ── The copy-only audit, end to end ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_copy_only_overrun_is_reported_in_provenance():
    """The budget check is only a check if something downstream can see
    whether it held. Nothing is cut — see copy_only.audit_plan_shape."""
    cases = [{"title": f"case {i}", "steps": ["s"], "expected": "e",
              "priority": "medium"} for i in range(11)]
    plan = _plan(cases, copy_only={
        "verdict": "copy_only",
        "variant_count": 3,
        "rationale": "strings plus a hook signature change",
        "unreachable_variants": ["brokerage banner — needs a brokerage-level account"],
    })

    with (
        patch.object(plan_service.JiraClient, "download_image_as_base64",
                     AsyncMock(return_value=None)),
        patch.object(plan_service, "get_llm_client", return_value=_stub_llm(plan)),
    ):
        response = await plan_service.generate_single(_request())

    audit = response["source_provenance"]["copy_only"]
    assert audit["budget"] == 7
    assert audit["manual_cases"] == 11
    assert audit["within_budget"] is False
    assert audit["unreachable_variants"] == [
        "brokerage banner — needs a brokerage-level account"
    ]
    assert len(response["happy_path"]) == 11, "the audit must not cut cases"


@pytest.mark.asyncio
async def test_no_copy_only_key_when_the_model_did_not_classify_one():
    """A plan that never saw the rule must not carry a copy-only verdict —
    an absent key and a 'not copy-only' verdict mean different things."""
    with (
        patch.object(plan_service.JiraClient, "download_image_as_base64",
                     AsyncMock(return_value=None)),
        patch.object(plan_service, "get_llm_client", return_value=_stub_llm(_plan())),
    ):
        response = await plan_service.generate_single(_request())

    assert "copy_only" not in response["source_provenance"]
