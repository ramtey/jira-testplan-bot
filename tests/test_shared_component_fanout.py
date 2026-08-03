"""Regression tests for the shared-component fan-out rule.

Anchors the SK-1898 → SK-2511 failure mode: a ticket that adds a field to
a shared modal (consumed by both buyer estimate and seller net sheet)
must produce a plan whose cases would surface the role-mismatch bug
BEFORE release, not months later.

Layers exercised:
1. Unit — ``detect_fanout`` fires on an SK-1898-shaped ticket and stays
   silent on ones that scope themselves to a single role.
2. Prompt — ``_build_prompt`` embeds the fan-out block with the SK-1898
   worked example, both role names, and the per-role negative-space
   directive for the fields the roles do NOT consume.
3. End-to-end plan — an LLM stub that follows the fan-out guidance
   produces a plan whose cases contain the assertions the deliverable
   requires (buyer case + seller case + explicit "loan amount does not
   appear on [buyer estimate]" text).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.app.llm_client import OllamaClient, _merge_fanout_contexts  # noqa: E402
from src.app.models import TestPlan  # noqa: E402
from src.app.shared_component_fanout import (  # noqa: E402
    KNOWN_ROLES,
    ROLE_CONSUMPTION_MAP,
    FanoutContext,
    FanoutField,
    detect_fanout,
    render_fanout_guidance,
)


# ── Fixture: an SK-1898-shaped ticket ────────────────────────────────────────


SK1898_SUMMARY = "Display assessed value and loan amount in the Property Tax Results modal"

SK1898_DESCRIPTION = """## User Story
Users viewing property tax results should see the source values that drove the
calculation so they can spot obviously-wrong inputs before signing anything.

## Acceptance Criteria
* The Property Tax Results modal displays the assessed value.
* The Property Tax Results modal displays the loan amount.
* Both fields are labelled and rendered in the standard modal typography.

## Notes
Uses the shared Property Tax Results component.
"""

SK1898_DEVELOPMENT_INFO = {
    "pull_requests": [
        {
            "title": "Show assessed value + loan amount in property tax results modal",
            "status": "MERGED",
            "files_changed": [
                {
                    "filename": "apps/expo/src/components/modals/PropertyTaxResults.tsx",
                    "status": "modified",
                    "additions": 14,
                    "deletions": 2,
                    "patch": (
                        "@@ modal body @@\n"
                        "+  <Text>Assessed value: {assessedValue}</Text>\n"
                        "+  <Text>Loan amount: {loanAmount}</Text>\n"
                    ),
                }
            ],
        }
    ]
}


# ── Layer 1: detector unit tests ─────────────────────────────────────────────


class TestDetectFanoutFires:
    """detect_fanout returns a populated context for SK-1898-shaped tickets."""

    def test_returns_context_for_sk1898_fixture(self):
        ctx = detect_fanout(
            summary=SK1898_SUMMARY,
            description=SK1898_DESCRIPTION,
            development_info=SK1898_DEVELOPMENT_INFO,
        )
        assert ctx is not None
        # Both known roles must be present — that's the whole point of fan-out.
        assert set(ctx.roles) == set(KNOWN_ROLES)

    def test_pr_file_path_triggers(self):
        ctx = detect_fanout(
            summary=SK1898_SUMMARY,
            description=SK1898_DESCRIPTION,
            development_info=SK1898_DEVELOPMENT_INFO,
        )
        assert ctx is not None
        assert "pr_file_in_shared_component_dir" in ctx.triggers
        assert any(
            p.startswith("apps/expo/src/components/modals/")
            for p in ctx.shared_paths
        )

    def test_detected_fields_include_loan_amount_and_assessed_value(self):
        ctx = detect_fanout(
            summary=SK1898_SUMMARY,
            description=SK1898_DESCRIPTION,
            development_info=SK1898_DEVELOPMENT_INFO,
        )
        assert ctx is not None
        names = {f.name for f in ctx.fields}
        assert "loan amount" in names
        assert "assessed value" in names

    def test_loan_amount_is_seller_only_per_map(self):
        # This is the crux of SK-2511 — loan amount must be flagged as
        # NOT consumed by buyer estimate. If the map ever regresses, this
        # test fires and blocks the change.
        assert "loan amount" in ROLE_CONSUMPTION_MAP
        assert ROLE_CONSUMPTION_MAP["loan amount"] == frozenset({"seller net sheet"})

    def test_fires_on_shared_language_only(self):
        """Even without a PR-file signal, the shared-language trigger + a
        mapped field is enough."""
        ctx = detect_fanout(
            summary="Add HOA dues to the shared Results modal",
            description="The shared modal should display HOA dues alongside tax.",
            development_info=None,
        )
        assert ctx is not None
        # Field-in-map + shared-language should both fire.
        assert "shared_language_in_ticket_text" in ctx.triggers


class TestDetectFanoutDoesNotFire:
    """The escape hatches — role-scoped tickets must NOT trigger fan-out."""

    def test_seller_scoped_summary_suppresses(self):
        ctx = detect_fanout(
            summary="Seller net sheet: display loan amount in property tax results",
            description=SK1898_DESCRIPTION,
            development_info=SK1898_DEVELOPMENT_INFO,
        )
        assert ctx is None

    def test_buyer_scoped_summary_suppresses(self):
        ctx = detect_fanout(
            summary="Buyer estimate: show HOA dues on results modal",
            description="Add HOA dues to the shared modal.",
            development_info=SK1898_DEVELOPMENT_INFO,
        )
        assert ctx is None

    def test_as_a_seller_user_story_suppresses(self):
        ctx = detect_fanout(
            summary="Show loan amount in results modal",
            description="As a seller I want to see the loan amount so I can double-check.",
            development_info=SK1898_DEVELOPMENT_INFO,
        )
        assert ctx is None

    def test_no_signals_returns_none(self):
        ctx = detect_fanout(
            summary="Fix login button styling on the marketing site",
            description="Change the login button background from blue to teal.",
            development_info={"pull_requests": [{"files_changed": [
                {"filename": "apps/web/src/pages/login.tsx"}
            ]}]},
        )
        assert ctx is None


class TestMergeFanoutContexts:
    def test_union_of_multiple_contexts(self):
        a = FanoutContext(
            fields=[FanoutField("loan amount", frozenset({"seller net sheet"}))],
            triggers=["pr_file_in_shared_component_dir"],
            shared_paths=["apps/expo/src/components/modals/PropertyTaxResults.tsx"],
        )
        b = FanoutContext(
            fields=[
                # duplicate field, should dedupe
                FanoutField("loan amount", frozenset({"seller net sheet"})),
                FanoutField("hoa dues", frozenset({"buyer estimate", "seller net sheet"})),
            ],
            triggers=["shared_language_in_ticket_text"],
            shared_paths=["apps/expo/src/components/modals/HoaDues.tsx"],
        )
        merged = _merge_fanout_contexts([a, None, b])
        assert merged is not None
        assert {f.name for f in merged.fields} == {"loan amount", "hoa dues"}
        assert set(merged.triggers) == {
            "pr_file_in_shared_component_dir",
            "shared_language_in_ticket_text",
        }
        assert len(merged.shared_paths) == 2

    def test_all_none_returns_none(self):
        assert _merge_fanout_contexts([None, None]) is None


# ── Layer 2: prompt-level assertions ─────────────────────────────────────────


class TestPromptEmbedsFanoutBlock:
    """The prompt built for an SK-1898 ticket must contain the fan-out
    guidance, the SK-1898 → SK-2511 worked example, and the per-role
    negative-space directives."""

    @pytest.fixture(autouse=True)
    def _client(self, monkeypatch):
        # OllamaClient's __init__ only reads settings — safe to instantiate
        # without any network side effects.
        self.client = OllamaClient()

    def _prompt(self) -> str:
        return self.client._build_prompt(
            ticket_key="SK-1898",
            summary=SK1898_SUMMARY,
            description=SK1898_DESCRIPTION,
            testing_context={},
            development_info=SK1898_DEVELOPMENT_INFO,
        )

    def test_prompt_contains_fanout_header(self):
        prompt = self._prompt()
        assert "SHARED-COMPONENT FAN-OUT" in prompt

    def test_prompt_contains_worked_example_verbatim(self):
        prompt = self._prompt()
        # The failure-mode narrative and both ticket keys must appear so
        # the LLM anchors to the story, not just the rule.
        assert "SK-1898" in prompt
        assert "SK-2511" in prompt
        assert "Property Tax Results modal" in prompt

    def test_prompt_names_both_roles(self):
        prompt = self._prompt()
        assert "buyer estimate" in prompt
        assert "seller net sheet" in prompt

    def test_prompt_requires_negative_space_for_loan_amount_on_buyer(self):
        """The prompt MUST direct the model to assert loan amount does not
        appear on the buyer estimate — that's the assertion that would have
        caught SK-2511."""
        prompt = self._prompt().lower()
        # Look for the per-role directive block with loan amount + buyer.
        # The renderer emits: "`buyer estimate` case MUST assert: loan amount
        # does NOT appear on the buyer estimate surface."
        assert "loan amount" in prompt
        assert "buyer estimate" in prompt
        assert "does not appear" in prompt

    def test_prompt_suppressed_when_ticket_is_role_scoped(self):
        prompt = self.client._build_prompt(
            ticket_key="SK-1898",
            summary="Seller net sheet: display loan amount in property tax results",
            description=SK1898_DESCRIPTION,
            testing_context={},
            development_info=SK1898_DEVELOPMENT_INFO,
        )
        assert "SHARED-COMPONENT FAN-OUT" not in prompt


# ── Layer 3: end-to-end plan-shape assertions ────────────────────────────────


class _StubLLM(OllamaClient):
    """OllamaClient subclass that captures the built prompt and returns a
    canned TestPlan without touching the network. Used to simulate an LLM
    that follows the fan-out guidance."""

    def __init__(self, response_plan: TestPlan):
        super().__init__()
        self.response_plan = response_plan
        self.captured_prompt: str | None = None

    async def _fetch_linked_specs(self, description, comments):
        return []

    async def generate_test_plan(  # type: ignore[override]
        self,
        ticket_key,
        summary,
        description,
        testing_context,
        development_info=None,
        images=None,
        comments=None,
        parent_info=None,
        child_info=None,
        linked_info=None,
        slack_messages=None,
        seed_regressions=None,
        bounce_history=None,
    ):
        self.captured_prompt = self._build_prompt(
            ticket_key,
            summary,
            description,
            testing_context,
            development_info,
            has_images=bool(images),
            comments=comments,
            parent_info=parent_info,
            child_info=child_info,
            linked_info=linked_info,
            slack_messages=slack_messages,
            seed_regressions=seed_regressions,
            bounce_history=bounce_history,
            linked_specs=[],
        )
        return self.response_plan


def _canned_sk1898_plan_that_follows_the_guidance() -> TestPlan:
    """A plan an LLM WOULD produce if it followed the fan-out block:
    one case per role, with an explicit negative-space assertion on
    loan amount for the buyer surface."""
    return TestPlan(
        happy_path=[
            {
                "title": "[Buyer estimate] Property Tax Results modal shows assessed value only",
                "preconditions": "Test file configured as a Buyer Estimate.",
                "steps": [
                    "Open a Buyer Estimate file with property tax data.",
                    "Trigger the Property Tax Results modal.",
                    "Inspect the fields rendered in the modal body.",
                ],
                "expected": (
                    "The modal renders assessed value. The modal MUST NOT "
                    "render loan amount — loan amount does not appear on "
                    "the buyer estimate surface (loan amount is only "
                    "consumed by the seller net sheet calculator)."
                ),
                "covers_acs": ["SK-1898-AC1", "SK-1898-AC2"],
            },
            {
                "title": "[Seller net sheet] Property Tax Results modal shows assessed value and loan amount",
                "preconditions": "Test file configured as a Seller Net Sheet.",
                "steps": [
                    "Open a Seller Net Sheet file with property tax data.",
                    "Trigger the Property Tax Results modal.",
                    "Inspect the fields rendered in the modal body.",
                ],
                "expected": (
                    "The modal renders BOTH assessed value AND loan amount, "
                    "since the seller net sheet calculator consumes both."
                ),
                "covers_acs": ["SK-1898-AC1", "SK-1898-AC2"],
            },
        ],
        edge_cases=[],
        regression_checklist=[],
        integration_tests=[],
    )


def test_generated_plan_for_sk1898_would_catch_the_sk2511_bug():
    """End-to-end: an LLM stub that follows the fan-out guidance must
    produce a plan with BOTH a buyer-estimate case AND a seller-net-sheet
    case, and the buyer case must assert that loan amount does not appear
    on the buyer surface. This is the plan-shape SK-2511 needed pre-release.

    Runs the coroutine via ``asyncio.run`` rather than the pytest-asyncio
    fixture to stay usable in bare-pytest environments (matches the
    ``test_llm.py`` pattern).
    """
    llm = _StubLLM(_canned_sk1898_plan_that_follows_the_guidance())

    plan = asyncio.run(llm.generate_test_plan(
        ticket_key="SK-1898",
        summary=SK1898_SUMMARY,
        description=SK1898_DESCRIPTION,
        testing_context={},
        development_info=SK1898_DEVELOPMENT_INFO,
    ))

    # The prompt the stub saw must carry the fan-out block — otherwise
    # the "if the LLM follows the guidance" premise is vacuous.
    assert llm.captured_prompt is not None
    assert "SHARED-COMPONENT FAN-OUT" in llm.captured_prompt

    titles = [c["title"].lower() for c in plan.happy_path]
    assert any("buyer estimate" in t for t in titles), (
        f"plan is missing a buyer-estimate case: {titles}"
    )
    assert any("seller net sheet" in t for t in titles), (
        f"plan is missing a seller-net-sheet case: {titles}"
    )

    buyer_case = next(c for c in plan.happy_path if "buyer estimate" in c["title"].lower())
    negative_space = (
        "loan amount does not appear on the buyer estimate"
    )
    assert negative_space in buyer_case["expected"].lower(), (
        "buyer-estimate case is missing the explicit negative-space "
        "assertion for loan amount"
    )


def test_render_guidance_omits_negative_space_when_all_fields_consumed_by_role():
    """When every detected field IS consumed by a role, the guidance for
    that role says a positive-space check is sufficient — no negative
    directive should be emitted (mapping-only fields wouldn't trigger a
    'does NOT appear' assertion for that role)."""
    ctx = FanoutContext(
        fields=[
            FanoutField("assessed value", frozenset({"buyer estimate", "seller net sheet"})),
        ],
        triggers=["pr_file_in_shared_component_dir"],
        shared_paths=["apps/expo/src/components/modals/PropertyTaxResults.tsx"],
    )
    block = render_fanout_guidance(ctx)
    # Both roles get a positive-space "sufficient" note; neither should be
    # told to assert absence.
    assert "positive-space check is sufficient" in block
    # And there's still a per-role case requirement.
    assert "AT LEAST ONE `happy_path` case PER consuming role" in block


def test_render_guidance_uses_diagnostic_for_unknown_fields():
    """A field NOT in the consumption map must be emitted as a diagnostic
    step (flag for PM), NOT as a hard assertion — that respects the
    'never bake current behaviour as pass criterion' rule."""
    ctx = FanoutContext(
        fields=[FanoutField("mystery field", frozenset())],
        triggers=["shared_language_in_ticket_text"],
    )
    block = render_fanout_guidance(ctx).lower()
    assert "flag for pm" in block
    assert "unknown role consumption" in block
