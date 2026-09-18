"""The copy-only plan-shape rule, and the safety property it rests on.

The failure this pins down: a ticket whose whole diff is user-visible
strings used to get the generator's default shape — one case per AC, per
route, per state. Six changed sentences produced a checklist with a case
per route to the same dialog, a case per retired string, a layout case
per variant, and a tail of "does saving still work" cases the PR's own
component tests already asserted. QA ran thirty cases to read six
sentences.

Three layers, and the middle one is the point:

1. Unit — ``detect_copy_only`` finds the evidence, and (more
   importantly) refuses to find it when the diff is unreadable or when a
   behavioural line changed. Detection is deliberately *evidence*, never
   a verdict: the block it triggers makes the model classify.

2. Prompt — the block reaches a single-ticket prompt and never reaches a
   multi-ticket one, because "(this ticket's variants) + 4" is not a
   number that means anything across a batch.

3. Audit — ``audit_plan_shape`` scores the emitted plan against the
   budget the model said it was working to, so "budget check before
   emitting" is something the replay eval can measure rather than
   something everyone assumes happened.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.app.copy_only import (  # noqa: E402
    DECLARED_COPY_LINE_RATIO,
    CopyOnlyContext,
    audit_plan_shape,
    case_budget,
    detect_copy_only,
    extract_copy_literals,
    is_copy_literal,
    render_copy_only_guidance,
)
from src.app.llm_client import SUBMIT_TEST_PLAN_TOOL, OllamaClient  # noqa: E402
from src.app.models import TestPlan  # noqa: E402


# ── Fixtures: diffs of each shape ────────────────────────────────────────────


# A string edit and nothing else: the two sides normalize identically once
# the literal contents are blanked out.
COPY_PATCH = """@@ -12,8 +12,8 @@ export function LeaveGroupDialog({ scope }) {
-  const title = 'Leave this group?'
-  const body = 'You will lose access to shared documents.'
-  const confirmLabel = 'Leave group'
+  const title = 'Leave this group?'
+  const body = 'You will lose access to shared documents and any drafts you have not sent.'
+  const confirmLabel = 'Leave anyway'
"""

# The accompanying refactor the rule explicitly allows: the hook's call
# signature changes, what it does does not.
REFACTOR_PATCH = """@@ -3,7 +3,7 @@
-export function useLeaveDialog(scope, role) {
-  return buildDialog(scope, role)
+export function useLeaveDialog({ scope, role }) {
+  return buildDialog({ scope, role })
 }
"""

# A reachable branch moves. Not copy under any reading.
BEHAVIOUR_PATCH = """@@ -4,4 +4,7 @@
-  if (memberCount > 0) return null
+  if (memberCount > 1) return null
+  const penalty = computeExitPenalty(memberCount)
+  await persistExit(penalty)
"""

LOCALE_PATCH = """@@ -18,3 +18,5 @@
+  "leaveGroup.title": "Leave this group?",
+  "leaveGroup.body": "You will lose access to shared documents."
"""


def _dev(files, *, pr_body="", title="Update the leave-group dialog", commits=None):
    return {
        "pull_requests": [
            {
                "title": title,
                "status": "MERGED",
                "github_description": pr_body,
                "files_changed": files,
            }
        ],
        "commits": [{"message": m} for m in (commits or [])],
    }


def _file(name, patch, changes=None):
    entry = {"filename": name, "patch": patch}
    if changes is not None:
        entry["changes"] = changes
    return entry


# ── 1. Detection: what counts as evidence ────────────────────────────────────


def test_string_only_diff_is_detected_with_its_variants_and_retirements():
    """The strong case: every changed non-test line differs only inside a
    string literal, so the detector can name both the new copy and the
    copy that was retired."""
    ctx = detect_copy_only(
        "Update leave-group dialog copy",
        "Reword the confirmation.",
        _dev([_file("src/components/LeaveGroupDialog.tsx", COPY_PATCH)]),
    )

    assert ctx is not None
    assert ctx.signal == "strings_only"
    assert "You will lose access to shared documents and any drafts you have not sent." in ctx.added_strings
    # Retired copy is what rule 2's single sweep case has to list.
    assert ctx.removed_strings == [
        "You will lose access to shared documents.",
        "Leave group",
    ]
    # A string present on both sides moved or reindented — it is not retired.
    assert "Leave this group?" not in ctx.removed_strings


def test_a_behavioural_line_defeats_detection_even_next_to_copy():
    """A diff that changes a reachable branch is not copy-only, and no
    amount of adjacent string editing makes it so. Nothing in the ticket
    claims copy here, so there is no declared-phrase escape hatch either."""
    ctx = detect_copy_only(
        "Leave-group dialog",
        "Update the dialog.",
        _dev([
            _file("src/components/LeaveGroupDialog.tsx", COPY_PATCH),
            _file("src/logic/exit.ts", BEHAVIOUR_PATCH),
        ]),
    )
    assert ctx is None


def test_an_unreadable_diff_is_never_evidence():
    """A PR whose patches GitHub did not return says nothing about what
    changed. Shrinking a plan on the strength of a diff nobody read is the
    worst outcome this module could produce, so no patch means no block —
    the same reflex as `require_source_grounding`."""
    ctx = detect_copy_only(
        "Update dialog copy",
        "Copy only, no behaviour change.",
        _dev([{"filename": "src/components/LeaveGroupDialog.tsx", "changes": 6}]),
    )
    assert ctx is None


def test_tests_are_inside_the_definition_not_evidence_against_it():
    """The rule says copy-only covers the strings "plus their tests". A
    copy PR that adds 200 lines of component tests is still copy-only, and
    the test files are reported so the prompt can tell the model to read
    them before writing a wiring case."""
    ctx = detect_copy_only(
        "Update leave-group dialog copy",
        "Reword.",
        _dev([
            _file("src/components/LeaveGroupDialog.tsx", COPY_PATCH),
            _file("src/components/__tests__/LeaveGroupDialog.test.tsx", BEHAVIOUR_PATCH),
        ]),
    )
    assert ctx is not None
    assert ctx.signal == "strings_only"
    assert ctx.test_files == ["src/components/__tests__/LeaveGroupDialog.test.tsx"]
    assert ctx.other_files == []


def test_an_accompanying_refactor_needs_the_ticket_to_say_it_is_copy():
    """The rule counts a signature refactor alongside a copy change as
    copy-only. The detector cannot tell that refactor from a behaviour
    change by shape alone, so it requires the ticket or PR to assert the
    scope — and then still leaves the judgement to the model."""
    files = [
        _file("src/components/LeaveGroupDialog.tsx", COPY_PATCH),
        _file("src/hooks/useLeaveDialog.ts", REFACTOR_PATCH),
    ]

    silent = detect_copy_only("Leave-group dialog", "Update the dialog.", _dev(files))
    assert silent is None, "a mixed diff with no scope statement is not evidence"

    declared = detect_copy_only(
        "Leave-group dialog",
        "Update the dialog.",
        _dev(files, pr_body="Copy only — the hook now takes an options object."),
    )
    assert declared is not None
    assert declared.signal == "declared"
    assert declared.other_files == ["src/hooks/useLeaveDialog.ts"]
    assert "copy only" in declared.scope_phrases


def test_a_scope_claim_cannot_outvote_a_mostly_behavioural_diff():
    """"Copy only" in a PR body is a claim about intent, and intent drifts
    from diffs. Below DECLARED_COPY_LINE_RATIO the claim is treated as
    stale scope talk rather than as a description of this diff."""
    ctx = detect_copy_only(
        "Leave-group dialog",
        "Update the dialog.",
        _dev(
            [
                _file("src/components/LeaveGroupDialog.tsx", COPY_PATCH),
                _file("src/logic/exit.ts", BEHAVIOUR_PATCH * 4),
            ],
            pr_body="Copy only.",
        ),
    )
    assert DECLARED_COPY_LINE_RATIO == 0.5
    assert ctx is None


def test_a_locale_bundle_counts_as_copy_even_when_only_adding_keys():
    """Adding a key to a locale bundle is a pure addition — the added and
    removed lines cannot normalize to each other — so the file-path rule
    is what carries it."""
    ctx = detect_copy_only(
        "Add empty-state copy",
        "New strings.",
        _dev([_file("apps/web/locales/en.json", LOCALE_PATCH)]),
    )
    assert ctx is not None
    assert ctx.signal == "strings_only"
    assert "Leave this group?" in ctx.added_strings


@pytest.mark.parametrize(
    "literal,is_copy",
    [
        ("'Leave this group?'", True),
        ('"You will lose access."', True),
        ("'leaveGroup.title'", False),        # i18n key
        ("'../hooks/useLeaveDialog'", False),  # import path
        ("'#f5f5f5'", False),                  # hex colour
        ("'LEAVE_GROUP'", False),              # constant
        ("'text-sm'", False),                  # css class
        ("'42'", False),                       # number
        ("'ok'", False),                       # too short
    ],
)
def test_only_user_visible_looking_literals_become_candidate_variants(literal, is_copy):
    """Every identifier in a diff is a quoted string. Counting them as
    variants would inflate the budget the plan is sized against, which
    defeats the rule as surely as ignoring it."""
    assert is_copy_literal(literal) is is_copy


def test_literal_extraction_dedupes_and_keeps_diff_order():
    lines = [
        "  const a = 'Leave this group?'",
        "  const b = 'Leave this group?'",
        "  const c = 'You will lose access.'",
    ]
    assert extract_copy_literals(lines) == [
        "Leave this group?",
        "You will lose access.",
    ]


# ── 2. The prompt block ──────────────────────────────────────────────────────


def test_the_block_leads_with_the_disqualifiers_not_the_budget():
    """Detection is recall-biased on purpose. If the block opened with the
    case budget, a diff that merely looks like copy would get a
    copy-sized plan before the model considered whether behaviour changed.
    The classification gate has to come first, and it has to name the
    default for an unsure model."""
    ctx = detect_copy_only(
        "Update leave-group dialog copy",
        "Reword.",
        _dev([_file("src/components/LeaveGroupDialog.tsx", COPY_PATCH)]),
    )
    block = render_copy_only_guidance(ctx)

    gate = block.index("NOT copy-only if")
    budget = block.index("BUDGET CHECK BEFORE EMITTING")
    assert gate < budget
    assert 'If you are unsure, it is NOT copy-only' in block
    assert "IGNORE\nevery rule below" in block


def test_the_block_carries_the_extracted_evidence():
    ctx = detect_copy_only(
        "Update leave-group dialog copy",
        "Reword.",
        _dev([
            _file("src/components/LeaveGroupDialog.tsx", COPY_PATCH),
            _file("src/components/__tests__/LeaveGroupDialog.test.tsx", COPY_PATCH),
        ]),
    )
    block = render_copy_only_guidance(ctx)

    assert "Leave anyway" in block                               # new copy
    assert "Leave group" in block                                # retired copy
    assert "never one case each" in block                        # rule 2
    assert "LeaveGroupDialog.test.tsx" in block                  # read the tests first
    # Literals are candidates, not the variant count — several of them are
    # one dialog, which is rule 1's whole point.
    assert "These are literals, not variants" in block


def test_the_block_reaches_a_single_ticket_prompt_only():
    """"(variants) + 4" has no meaning over a batch that also carries a
    backend change, so the multi-ticket builder must never inject it."""
    client = OllamaClient()
    dev = _dev([_file("src/components/LeaveGroupDialog.tsx", COPY_PATCH)])

    single = client._build_prompt(
        "SK-1", "Update leave-group dialog copy", "Reword.", {}, dev
    )
    assert "COPY-ONLY TICKET RULE" in single

    multi = client._build_multi_ticket_prompt([
        {
            "ticket_key": "SK-1",
            "summary": "Update leave-group dialog copy",
            "description": "Reword.",
            "development_info": dev,
            "acceptance_criteria": ["Copy is updated"],
        }
    ])
    assert "COPY-ONLY TICKET RULE" not in multi


def test_a_behavioural_ticket_gets_no_block():
    client = OllamaClient()
    prompt = client._build_prompt(
        "SK-1",
        "Charge an exit penalty",
        "Members leaving a group are charged.",
        {},
        _dev([_file("src/logic/exit.ts", BEHAVIOUR_PATCH)]),
    )
    assert "COPY-ONLY TICKET RULE" not in prompt


def test_the_tool_schema_asks_for_the_verdict_either_way():
    """A 'not_copy_only' with a rationale is a result. If the schema only
    accepted the positive verdict, a model that quietly declined the rule
    would be indistinguishable from one that never saw it."""
    field = SUBMIT_TEST_PLAN_TOOL["input_schema"]["properties"]["copy_only"]
    assert field["properties"]["verdict"]["enum"] == ["copy_only", "not_copy_only"]
    assert field["required"] == ["verdict", "rationale"]
    assert "unreachable_variants" in field["properties"]


# ── 3. The budget audit ──────────────────────────────────────────────────────


def test_case_budget_is_variants_plus_four():
    assert case_budget(0) == 4
    assert case_budget(3) == 7


def _plan_with(cases, copy_only=None):
    return TestPlan(
        happy_path=cases,
        edge_cases=[],
        regression_checklist=[],
        copy_only=copy_only,
    )


def test_no_audit_when_the_model_did_not_call_it_copy_only():
    """A plan that was never meant to be copy-shaped must not be reported
    as over a budget it was never given."""
    assert audit_plan_shape(_plan_with([{"title": "a"}])) is None
    assert audit_plan_shape(
        _plan_with(
            [{"title": "a"}],
            copy_only={"verdict": "not_copy_only", "rationale": "adds a branch"},
        )
    ) is None


def test_a_plan_within_its_budget_is_recorded_as_such():
    plan = _plan_with(
        [{"title": f"case {i}"} for i in range(6)],
        copy_only={
            "verdict": "copy_only",
            "variant_count": 3,
            "rationale": "strings plus the hook signature",
        },
    )
    audit = audit_plan_shape(plan)
    assert audit["budget"] == 7
    assert audit["manual_cases"] == 6
    assert audit["within_budget"] is True


def test_an_overrun_is_reported_and_the_plan_is_left_alone():
    """Deciding WHICH case to cut is a judgement the rule assigns to the
    model — "cut from the exclusion list, never from rule 1". A mechanical
    trim would drop whichever cases sorted last, including the variant
    cases the rule protects. So the audit reports and does not edit."""
    cases = [{"title": f"case {i}"} for i in range(11)]
    plan = _plan_with(
        cases,
        copy_only={"verdict": "copy_only", "variant_count": 3, "rationale": "copy"},
    )
    audit = audit_plan_shape(plan)

    assert audit["within_budget"] is False
    assert audit["manual_cases"] == 11
    assert plan.happy_path == cases, "the audit must not edit the plan"


def test_unit_test_covered_cases_do_not_count_against_the_budget():
    """The exclusion list tells the model to mark persistence and wiring
    `covered_by_unit_test` and keep them off the manual checklist.
    Counting those against the budget would penalise exactly the
    behaviour the rule asks for."""
    plan = _plan_with(
        [
            {"title": "variant A"},
            {"title": "variant B"},
            {"title": "save still works", "covered_by_unit_test": True},
            {"title": "payload shape", "covered_by_unit_test": True},
        ],
        copy_only={"verdict": "copy_only", "variant_count": 2, "rationale": "copy"},
    )
    assert audit_plan_shape(plan)["manual_cases"] == 2


def test_a_missing_variant_count_leaves_the_budget_unjudged():
    """`variant_count` is only required-by-convention, not by the schema.
    Absent it, the honest answer is "no verdict" — not a budget of 4 that
    the model never agreed to."""
    audit = audit_plan_shape(
        _plan_with(
            [{"title": "a"}],
            copy_only={"verdict": "copy_only", "rationale": "copy"},
        )
    )
    assert audit["budget"] is None
    assert audit["within_budget"] is None


def test_unreachable_variants_survive_into_the_audit():
    """Fixture honesty. QA has to learn a variant is unreachable at the
    top of the plan, not by working down to a case no account can run."""
    audit = audit_plan_shape(
        _plan_with(
            [{"title": "a"}],
            copy_only={
                "verdict": "copy_only",
                "variant_count": 2,
                "rationale": "copy",
                "unreachable_variants": ["multi-group banner — needs an account managing 2+ groups"],
            },
        )
    )
    assert audit["unreachable_variants"] == [
        "multi-group banner — needs an account managing 2+ groups"
    ]
