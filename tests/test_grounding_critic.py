"""Tests for the post-generation grounding critic — SK-2290-style hallucinations.

The critic reads each test case's (title, steps, expected) alongside the
verbatim text of every AC ID it cites and returns a grounded/ungrounded
verdict. Ungrounded cases are badged in place with
``needs_manual_verification=True`` and gain a ``grounding_warnings`` entry
so the frontend renders the existing "Ungrounded UI ref" badge.

Regression coverage is anchored on the real-world SK-2290 example: a case
titled "Audit log viewer correctly filters by date range" tagged against
an AC whose text is only "Audit history for a given user is viewable in
the admin dashboard." The critic must catch that hallucination.
"""

from unittest.mock import AsyncMock

import pytest

from src.app.grounding_critic import (
    apply_verdicts,
    build_ac_index,
    build_case_verification_inputs,
    build_critic_user_message,
    parse_verdicts,
)
from src.app.models import TestPlan


def _sk2290_plan() -> TestPlan:
    """Fixture based on the real SK-2290 v3 plan that triggered this bug.

    tc-edge_cases-3 tests a date-range filter that AC-15 never mentions.
    tc-happy_path-1 tests a behaviour AC-1 actually requires (control case
    to prove the critic doesn't badge everything).
    """
    return TestPlan(
        happy_path=[
            {
                "title": "Audit log records user creation events",
                "steps": [
                    "As an admin, create a new user account.",
                    "Open the admin activity log.",
                    "Confirm a row for the newly created user appears.",
                ],
                "expected": "The activity log shows the create event with actor, action, and timestamp.",
                "priority": "high",
                "covers_acs": ["SK-2290-AC1"],
            },
        ],
        edge_cases=[
            {
                "title": "Audit log viewer correctly filters by date range",
                "category": "boundary",
                "steps": [
                    "Open the admin activity log.",
                    "Set the date-range picker to the past 7 days.",
                    "Confirm only entries within the range are shown.",
                ],
                "expected": "Entries outside the selected date range are hidden.",
                "priority": "medium",
                "covers_acs": ["SK-2290-AC15"],
            },
        ],
        regression_checklist=[],
        integration_tests=[],
    )


def _sk2290_tickets() -> list[dict]:
    return [
        {
            "ticket_key": "SK-2290",
            "acceptance_criteria": [
                "Every user-facing action is written to a persistent audit log.",
                # ...ACs 2–14 elided; only AC1 and AC15 are relevant to the fixture...
                *["placeholder AC"] * 13,
                "Audit history for a given user is viewable in the admin dashboard",
            ],
        }
    ]


# ─── build_ac_index ───────────────────────────────────────────────────────────


def test_build_ac_index_numbers_from_one():
    """AC IDs mirror how _compute_ac_coverage numbers them (starts at AC1)."""
    tickets = [
        {"ticket_key": "SK-1", "acceptance_criteria": ["first", "second"]},
        {"ticket_key": "SK-2", "acceptance_criteria": ["only one"]},
    ]
    index = build_ac_index(tickets)
    assert index == {
        "SK-1-AC1": "first",
        "SK-1-AC2": "second",
        "SK-2-AC1": "only one",
    }


def test_build_ac_index_skips_blank_and_non_string_entries():
    tickets = [
        {"ticket_key": "SK-1", "acceptance_criteria": ["  ", "real one", None, 42, "another"]},
    ]
    index = build_ac_index(tickets)
    # Blanks are dropped BUT the numbering is positional so a blank still consumes
    # a slot — this matches how _compute_ac_coverage numbers, which is what the
    # LLM's covers_acs tags reference.
    assert "SK-1-AC2" in index and index["SK-1-AC2"] == "real one"
    assert "SK-1-AC5" in index and index["SK-1-AC5"] == "another"


def test_build_ac_index_ignores_tickets_without_key():
    tickets = [
        {"acceptance_criteria": ["orphan"]},
        {"ticket_key": "SK-1", "acceptance_criteria": ["kept"]},
    ]
    assert build_ac_index(tickets) == {"SK-1-AC1": "kept"}


# ─── build_case_verification_inputs ───────────────────────────────────────────


def test_build_case_verification_inputs_pairs_cases_with_cited_ac_texts():
    plan = _sk2290_plan()
    ac_index = build_ac_index(_sk2290_tickets())
    inputs = build_case_verification_inputs(plan, ac_index)

    ids = {c["case_id"] for c in inputs}
    assert "happy_path:0" in ids
    assert "edge_cases:0" in ids

    edge = next(c for c in inputs if c["case_id"] == "edge_cases:0")
    assert edge["title"] == "Audit log viewer correctly filters by date range"
    assert any("date-range" in s for s in edge["steps"])
    assert edge["cited_acs"] == [
        {
            "ac_id": "SK-2290-AC15",
            "text": "Audit history for a given user is viewable in the admin dashboard",
        }
    ]


def test_build_case_verification_inputs_skips_cases_with_no_cited_acs():
    """A case with empty covers_acs has nothing to verify against — the
    separate 'Untraced' badge handles those. The critic should skip them."""
    plan = TestPlan(
        happy_path=[{"title": "no ac", "covers_acs": [], "steps": ["do a thing"]}],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
    )
    ac_index = {"SK-1-AC1": "text"}
    assert build_case_verification_inputs(plan, ac_index) == []


def test_build_case_verification_inputs_skips_unknown_ac_ids():
    """When every cited ID is missing from the AC index, the case has nothing
    to check against and is skipped."""
    plan = TestPlan(
        happy_path=[{"title": "ghost", "covers_acs": ["SK-9-AC42"], "steps": []}],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
    )
    ac_index = {"SK-1-AC1": "known"}
    assert build_case_verification_inputs(plan, ac_index) == []


def test_build_case_verification_inputs_skips_already_flagged_cases():
    """The generator sometimes marks a case ungrounded itself. Don't spend
    critic budget re-checking those — the badge is already there."""
    plan = TestPlan(
        happy_path=[
            {
                "title": "already ungrounded",
                "covers_acs": ["SK-1-AC1"],
                "steps": ["a step"],
                "needs_manual_verification": True,
            }
        ],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
    )
    assert build_case_verification_inputs(plan, {"SK-1-AC1": "text"}) == []


def test_build_critic_user_message_includes_cited_ac_text():
    """The critic MUST see the AC text verbatim — that's the whole point."""
    plan = _sk2290_plan()
    ac_index = build_ac_index(_sk2290_tickets())
    inputs = build_case_verification_inputs(plan, ac_index)
    message = build_critic_user_message(inputs)

    assert "Audit log viewer correctly filters by date range" in message
    assert "SK-2290-AC15" in message
    assert (
        "Audit history for a given user is viewable in the admin dashboard"
        in message
    )
    # Steps are surfaced so the critic can judge the actual claim, not just the title.
    assert "date-range picker" in message


# ─── apply_verdicts ───────────────────────────────────────────────────────────


def test_apply_verdicts_badges_the_sk2290_hallucination():
    """Regression: the SK-2290 tc-edge_cases-3 case must be badged when the
    critic marks it ungrounded — the AC only says 'viewable', not 'filter by
    date range'."""
    plan = _sk2290_plan()
    verdicts = {
        "happy_path:0": {"verdict": "grounded", "reason": "AC1 requires user-creation is logged."},
        "edge_cases:0": {
            "verdict": "ungrounded",
            "reason": "AC15 requires the log be viewable; it never mentions a date-range filter.",
        },
    }

    added = apply_verdicts(plan, verdicts)

    # The hallucinated case is badged.
    assert plan.edge_cases[0].get("needs_manual_verification") is True
    # The grounded case is left alone.
    assert not plan.happy_path[0].get("needs_manual_verification")

    # A matching grounding_warnings entry was appended.
    assert len(added) == 1
    assert added[0]["ac_id"] == "SK-2290-AC15"
    assert added[0]["missing_element"] == "Audit log viewer correctly filters by date range"
    assert "date-range" in added[0]["explanation"]
    assert plan.grounding_warnings == added


def test_apply_verdicts_preserves_existing_grounding_warnings():
    """If the LLM already emitted grounding_warnings, appending new critic-pass
    entries must not clobber them."""
    plan = _sk2290_plan()
    existing = [
        {"ac_id": "SK-2290-AC10", "missing_element": "some button", "explanation": "not in diff"},
    ]
    plan.grounding_warnings = list(existing)

    apply_verdicts(plan, {
        "edge_cases:0": {"verdict": "ungrounded", "reason": "AC15 has no filter."},
    })

    assert plan.grounding_warnings[0] == existing[0]
    assert plan.grounding_warnings[-1]["missing_element"] == (
        "Audit log viewer correctly filters by date range"
    )


def test_apply_verdicts_grounded_is_noop():
    plan = _sk2290_plan()
    verdicts = {
        "happy_path:0": {"verdict": "grounded", "reason": "fine"},
        "edge_cases:0": {"verdict": "grounded", "reason": "fine"},
    }
    added = apply_verdicts(plan, verdicts)
    assert added == []
    assert not plan.happy_path[0].get("needs_manual_verification")
    assert not plan.edge_cases[0].get("needs_manual_verification")
    assert not plan.grounding_warnings


def test_apply_verdicts_empty_verdicts_is_noop():
    plan = _sk2290_plan()
    assert apply_verdicts(plan, {}) == []
    assert not plan.grounding_warnings


def test_apply_verdicts_skips_case_with_no_cited_ac():
    """Can't attach a grounding_warning without an AC ID; skip rather than
    invent one."""
    plan = TestPlan(
        happy_path=[{"title": "no ac", "steps": ["x"], "covers_acs": []}],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
    )
    added = apply_verdicts(plan, {
        "happy_path:0": {"verdict": "ungrounded", "reason": "no anchor."},
    })
    assert added == []
    # The case isn't badged either — with no AC to attach to, the badge would
    # dangle. The 'Untraced' badge already handles this shape.
    assert not plan.happy_path[0].get("needs_manual_verification")


# ─── parse_verdicts ───────────────────────────────────────────────────────────


def test_parse_verdicts_accepts_tool_input_wrapper():
    raw = {"verdicts": [
        {"case_id": "happy_path:0", "verdict": "grounded", "reason": "ok"},
        {"case_id": "edge_cases:0", "verdict": "ungrounded", "reason": "no ac"},
    ]}
    out = parse_verdicts(raw)
    assert set(out.keys()) == {"happy_path:0", "edge_cases:0"}
    assert out["edge_cases:0"]["verdict"] == "ungrounded"


def test_parse_verdicts_drops_malformed_and_unknown_verdicts():
    raw = [
        "not a dict",
        {"case_id": "", "verdict": "grounded", "reason": ""},
        {"case_id": "x", "verdict": "maybe", "reason": ""},  # unknown enum
        {"case_id": "y", "verdict": "grounded", "reason": "ok"},  # keeper
    ]
    out = parse_verdicts(raw)
    assert list(out.keys()) == ["y"]


def test_parse_verdicts_missing_input_returns_empty():
    assert parse_verdicts(None) == {}
    assert parse_verdicts({"other": "shape"}) == {}


# ─── LLMClient default implementation is a no-op ──────────────────────────────


@pytest.mark.asyncio
async def test_default_llmclient_verify_case_grounding_is_empty():
    """The base LLMClient class returns {} — providers that don't override
    the method degrade gracefully (plan ships without extra badges)."""
    from src.app.llm_client import OllamaClient
    client = OllamaClient()
    result = await client.verify_case_grounding([
        {
            "case_id": "x",
            "title": "t",
            "steps": [],
            "expected": "",
            "test_data": "",
            "cited_acs": [{"ac_id": "SK-1-AC1", "text": "text"}],
        }
    ])
    assert result == {}


# ─── End-to-end integration through _run_grounding_critic ─────────────────────


@pytest.mark.asyncio
async def test_run_grounding_critic_badges_sk2290_case():
    """End-to-end: the helper called from main.py wires the pieces together
    and produces the expected mutations on the plan.
    """
    from src.app.main import _run_grounding_critic

    plan = _sk2290_plan()
    tickets = _sk2290_tickets()

    class _FakeLLM:
        async def verify_case_grounding(self, cases):
            # Assert the LLM was actually given the (case, AC text) pairing.
            assert any(c["case_id"] == "edge_cases:0" for c in cases)
            edge = next(c for c in cases if c["case_id"] == "edge_cases:0")
            assert edge["cited_acs"][0]["ac_id"] == "SK-2290-AC15"
            return {
                "happy_path:0": {"verdict": "grounded", "reason": "matches AC1."},
                "edge_cases:0": {
                    "verdict": "ungrounded",
                    "reason": "AC15 says the log is viewable; nothing about date-range filters.",
                },
            }

    await _run_grounding_critic(_FakeLLM(), plan, tickets)

    assert plan.edge_cases[0]["needs_manual_verification"] is True
    assert plan.happy_path[0].get("needs_manual_verification") is not True

    warnings = plan.grounding_warnings or []
    matching = [
        w for w in warnings if w.get("ac_id") == "SK-2290-AC15"
        and w.get("missing_element") == "Audit log viewer correctly filters by date range"
    ]
    assert len(matching) == 1
    assert "date-range" in matching[0]["explanation"]


@pytest.mark.asyncio
async def test_run_grounding_critic_swallows_llm_errors():
    """Critic failures must not crash the /generate-test-plan request —
    QA should get the un-badged plan, not a 500."""
    from src.app.main import _run_grounding_critic

    plan = _sk2290_plan()
    tickets = _sk2290_tickets()

    class _ExplodingLLM:
        async def verify_case_grounding(self, cases):
            raise RuntimeError("boom")

    # Must return cleanly.
    await _run_grounding_critic(_ExplodingLLM(), plan, tickets)
    assert not plan.edge_cases[0].get("needs_manual_verification")


@pytest.mark.asyncio
async def test_run_grounding_critic_noop_when_no_cases_to_verify():
    """When every case is missing covers_acs, don't even call the LLM."""
    from src.app.main import _run_grounding_critic

    plan = TestPlan(
        happy_path=[{"title": "no ac", "covers_acs": [], "steps": []}],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
    )
    llm = AsyncMock()
    llm.verify_case_grounding = AsyncMock(return_value={})
    await _run_grounding_critic(llm, plan, [{"ticket_key": "SK-1", "acceptance_criteria": ["a"]}])
    llm.verify_case_grounding.assert_not_called()
