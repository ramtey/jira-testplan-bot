"""Bounce-back history must reach BOTH prompt builders.

Detection is covered by test_bounce_detection.py; this file covers the step
after it — whether the detected history actually lands in the prompt.

The regression being pinned: bounce history reached the single-ticket prompt
only. `TicketInput` carried the field, but `plan_service.generate_multi` never
copied it into `tickets_data` and `_build_multi_ticket_prompt` rendered no
bounce section at all — so the coverage that guards against repeat failures
was missing from exactly the plans covering the most code. Nothing failed;
the section was just absent.
"""
from unittest.mock import AsyncMock, patch

import pytest

from src.app.llm_client import (
    _BOUNCE_USAGE_GUIDANCE,
    LLMClient,
    _render_bounce_entries,
)
from src.app.models import TicketInput

BOUNCE = {
    "from_status": "Ready for UAT",
    "to_status": "In Progress",
    "timestamp": "2026-08-01T10:00:00.000+0000",
    "author": "Kyle",
    "reason": "Loan amount showed on the buyer side.",
}

SECOND_BOUNCE = {
    "from_status": "In Testing",
    "to_status": "To Do",
    "timestamp": "2026-07-02T09:00:00.000+0000",
    "author": "Dana",
    "reason": "Totals were off by the transfer tax.",
}


def _ticket(key, *, bounces=None, acs=True):
    return {
        "ticket_key": key,
        "summary": f"{key} summary",
        "description": (
            "Acceptance Criteria:\n1. Something is displayed\n" if acs else "body"
        ),
        "issue_type": "Story",
        "testing_context": {},
        "development_info": None,
        "comments": None,
        "parent_info": None,
        "child_info": None,
        "linked_info": None,
        "bounce_history": bounces,
        "acceptance_criteria": ["Something is displayed"] if acs else [],
    }


class _PromptOnlyClient(LLMClient):
    """Concrete stand-in for the abstract base.

    Both prompt builders live on ``LLMClient`` and are pure string assembly,
    so testing them through this subclass exercises exactly the code the real
    Claude and Ollama clients call — without needing an API key or patching
    settings.
    """

    async def generate_test_plan(self, *a, **k): ...
    async def generate_multi_ticket_test_plan(self, *a, **k): ...
    async def generate_bug_analysis(self, *a, **k): ...
    async def generate_multi_bug_analysis(self, *a, **k): ...
    async def summarize_ticket(self, *a, **k): ...
    async def summarize_batch(self, *a, **k): ...
    async def summarize_bounce_reason(self, *a, **k): ...
    async def summarize_pr_changes(self, *a, **k): ...


def _client() -> LLMClient:
    return _PromptOnlyClient()


# ---------------------------------------------------------------------------
# The shared renderer
# ---------------------------------------------------------------------------


def test_renderer_includes_every_field_a_reviewer_needs():
    out = _render_bounce_entries([BOUNCE])
    assert "Ready for UAT → In Progress" in out
    assert "2026-08-01" in out
    assert "Kyle" in out
    assert "Loan amount showed on the buyer side." in out


def test_renderer_says_so_when_no_reason_was_paired():
    """A bounce with no nearby comment must read as 'reason unknown' rather
    than as a bounce with nothing wrong — the model needs to know it should
    still cover the flow end-to-end."""
    out = _render_bounce_entries([{**BOUNCE, "reason": None}])
    assert "reason unknown" in out.lower()


def test_renderer_labels_entries_with_the_ticket_in_multi_mode():
    """Pooling bounces across tickets is only safe if each stays attributed."""
    out = _render_bounce_entries([BOUNCE], label_prefix="SK-9 bounce")
    assert "**SK-9 bounce 1:**" in out


# ---------------------------------------------------------------------------
# Single-ticket prompt — unchanged by the shared-renderer extraction
# ---------------------------------------------------------------------------


def test_single_ticket_prompt_still_renders_the_bounce_section():
    prompt = _client()._build_prompt(
        "SK-1",
        "A ticket",
        "body",
        {},
        None,
        bounce_history=[BOUNCE],
    )
    assert "PRIOR QA / UAT BOUNCE-BACK HISTORY" in prompt
    assert "Ready for UAT → In Progress" in prompt
    assert "Loan amount showed on the buyer side." in prompt
    assert _BOUNCE_USAGE_GUIDANCE.strip().splitlines()[0] in prompt


def test_single_ticket_prompt_omits_the_section_with_no_bounces():
    prompt = _client()._build_prompt("SK-1", "A ticket", "body", {}, None)
    assert "PRIOR QA / UAT BOUNCE-BACK HISTORY" not in prompt


# ---------------------------------------------------------------------------
# Multi-ticket prompt — the gap this fixes
# ---------------------------------------------------------------------------


def test_multi_ticket_prompt_renders_bounce_history():
    prompt = _client()._build_multi_ticket_prompt(
        [_ticket("SK-1", bounces=[BOUNCE]), _ticket("SK-2")]
    )
    assert "PRIOR QA / UAT BOUNCE-BACK HISTORY" in prompt
    assert "Loan amount showed on the buyer side." in prompt
    assert _BOUNCE_USAGE_GUIDANCE.strip().splitlines()[0] in prompt


def test_multi_ticket_prompt_attributes_each_bounce_to_its_ticket():
    """Two tickets, two different failure modes. A pooled section that lost
    the attribution would let the model write coverage against the wrong
    ticket's flow."""
    prompt = _client()._build_multi_ticket_prompt(
        [
            _ticket("SK-1", bounces=[BOUNCE]),
            _ticket("SK-2", bounces=[SECOND_BOUNCE]),
        ]
    )
    assert "**SK-1 bounce 1:**" in prompt
    assert "**SK-2 bounce 1:**" in prompt
    assert "Loan amount showed on the buyer side." in prompt
    assert "Totals were off by the transfer tax." in prompt


def test_multi_ticket_prompt_omits_the_section_when_no_ticket_bounced():
    prompt = _client()._build_multi_ticket_prompt(
        [_ticket("SK-1"), _ticket("SK-2")]
    )
    assert "PRIOR QA / UAT BOUNCE-BACK HISTORY" not in prompt


def test_multi_ticket_prompt_caps_entries_per_ticket():
    """Five per ticket, matching the single-ticket cap, so a ticket with a
    long bounce history can't crowd out its siblings."""
    many = [{**BOUNCE, "reason": f"failure {i}"} for i in range(8)]
    prompt = _client()._build_multi_ticket_prompt([_ticket("SK-1", bounces=many)])
    assert "failure 4" in prompt
    assert "failure 5" not in prompt


# ---------------------------------------------------------------------------
# plan_service must carry the field through — the other half of the gap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_multi_passes_bounce_history_to_the_llm():
    """The prompt builder can only render what generate_multi hands it."""
    from src.app.services import plan_service

    captured: dict = {}

    class _Stub:
        async def classify_deliverable(self, **kwargs):
            return None

        async def generate_multi_ticket_test_plan(self, **kwargs):
            captured.update(kwargs)
            from src.app.models import TestPlan

            return TestPlan(happy_path=[], edge_cases=[], regression_checklist=[])

        async def verify_case_grounding(self, *a, **k):
            return []

        async def verify_code_grounding(self, *a, **k):
            return []

        async def verify_fix_scope(self, *a, **k):
            return []

        async def verify_surface_alignment(self, *a, **k):
            return []

    # Both tickets carry a merged PR: a batch with no grounded PR anywhere
    # short-circuits to the no-implementation result before the prompt is
    # built, and this test is about what reaches the prompt.
    def _merged_pr(number: int) -> dict:
        return {
            "pull_requests": [
                {
                    "title": f"PR {number}",
                    "status": "MERGED",
                    "url": f"https://github.com/acme/app/pull/{number}",
                    "repository": "acme/app",
                    "merged_at": "2026-09-01T10:00:00Z",
                }
            ],
            "commits": [],
            "branches": [],
        }

    tickets = [
        TicketInput(
            ticket_key="SK-1",
            summary="One",
            description="body",
            issue_type="Story",
            development_info=_merged_pr(1),
            bounce_history=[BOUNCE],
        ),
        TicketInput(
            ticket_key="SK-2",
            summary="Two",
            description="body",
            issue_type="Story",
            development_info=_merged_pr(2),
        ),
    ]

    with (
        patch.object(
            plan_service.JiraClient,
            "download_image_as_base64",
            AsyncMock(return_value=None),
        ),
        patch.object(plan_service, "get_llm_client", return_value=_Stub()),
    ):
        await plan_service.generate_multi(tickets)

    by_key = {t["ticket_key"]: t for t in captured["tickets"]}
    assert by_key["SK-1"]["bounce_history"] == [BOUNCE]
    assert by_key["SK-2"]["bounce_history"] is None
