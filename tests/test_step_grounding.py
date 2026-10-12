"""SK-2579: steps that name UI the product does not have.

Plan 610 (progress key SK-2579:5-1-0-14) shipped edge_cases:0 with step 6
"Link the same calculation to the same Forms file again using Connect". In the
Pilot mobile app a linked file offers only Disconnect
(agent-calculator apps/expo/src/hooks/useFormsFileLinkAffordance.ts), so the
step cannot be performed and a correct build failed UAT. The expected result
was badged unverified; the step was not. The same plan preconditioned its
cases on an ``autoSync`` flag that exists only in the ticket text, and its
regression line 6 asked a tester to rename a document in Forms, which files-ui
has no UI for.

Every case in that plan was grounded in ``ticket:SK-2579-desc`` alone — all
20 linked PRs were unreadable from GitHub that morning — and no guard looked
at step text. The cases below are copied verbatim from plan 610's stored body.
Plan 610 itself is not touched by anything here.
"""

import copy
from unittest.mock import AsyncMock, patch

import pytest

from src.app.llm_client import SYSTEM_PROMPT, TEST_CASE_SCHEMA, UI_GROUNDING_GUIDANCE
from src.app.models import GenerateTestPlanRequest, TestPlan
from src.app.regression_grounding_critic import (
    apply_regression_verdicts,
    line_id,
    names_a_control,
)
from src.app.services import plan_service
from src.app.services.progress_key import fingerprint
from src.app.services.test_plan_generator import (
    normalize_grounding_warnings,
    quarantine_ungrounded_cases,
)
from src.app.step_grounding import (
    MARKER,
    check_step_grounding,
    extract_named_references,
    find_unbacked_references,
    readable_code_paths,
)

AFFORDANCE_HOOK = "apps/expo/src/hooks/useFormsFileLinkAffordance.ts"

# ─── plan 610, verbatim ───────────────────────────────────────────────────────

EDGE_0 = {
    "title": "Document deleted in Forms: the calculation stops trying to update it and behaves as if nothing was ever saved",
    "priority": "critical",
    "category": "error_handling",
    "preconditions": "A calculation linked to a Forms file from the Pilot mobile app, autoSync on, with the net sheet PDF present on the file.",
    "steps": [
        "In Forms, delete the net sheet document from the file.",
        "In the mobile app, change a calculation input and let it save.",
        "Wait past the normal sync window, then reopen the calculation in the mobile app and use it normally — enter another input, navigate around the calculation.",
        "Observe whether any error state, stuck 'syncing' indication, or blocked control appears in the mobile app.",
        "Refresh the Forms file and confirm no net sheet document reappears.",
        "Link the same calculation to the same Forms file again using Connect, then change an input and wait for the sync window.",
        "Refresh the Forms file.",
    ],
    "expected": "Unverified — assumption from AC text and the ticket's job outline ('On 404 clear formsDocument'): after the delete, the first edit causes the calculation to drop its stored document link and stop attempting updates; nothing in the mobile app errors or hangs, and no document is recreated on the file by the edit alone. After re-linking, a net sheet PDF is uploaded to the file again and subsequent edits update it.",
    "surface": "mobile",
    "credentials": "Pilot mobile app test user session; Forms web access with delete permission on the file.",
    "expected_verified": False,
    "assertion_type": "behavior",
    "covers_acs": ["SK-2579-AC6"],
    "grounded_in": ["ticket:SK-2579-desc"],
}

HAPPY_1 = {
    "title": "Rapid successive edits produce exactly one final PDF matching the last value entered",
    "priority": "critical",
    "preconditions": "A calculation linked to a Forms file from the Pilot mobile app, autoSync on.",
    "steps": [
        "Note the current document on the Forms file (name and document ID) before starting.",
        "In the mobile app, change the purchase price three times in quick succession — value A, then value B, then value C — pausing less than a second between each change.",
        "Stop editing and wait for the background update to settle.",
        "Open the Forms file and list the documents on it.",
        "Open the net sheet document on the file and read the purchase price and cash-to-close it shows.",
    ],
    "expected": "Unverified — assumption: exactly one net sheet document, showing value C.",
    "surface": "mobile",
    "credentials": "Pilot mobile app test user session.",
    "expected_verified": False,
    "covers_acs": ["SK-2579-AC4"],
    "grounded_in": ["ticket:SK-2579-desc"],
}

REGRESSION_6 = (
    "🔴 Documents on a Forms file can still be uploaded, renamed, downloaded and "
    "deleted by a normal Forms user through the UI after the files-api routes "
    "moved to the Internal policy."
)


def _dev_info(*, readable: bool) -> list[dict]:
    """The shape plan_service hands the critics. On 2026-10-12 every PR came
    back ``github_enrichment_failed``; ``readable=True`` is the same PR with
    its diff, for the control cases."""
    pr = {
        "repository": "skyslope/agent-calculator",
        "number": 1342,
        "status": "MERGED",
        "merged_at": "2026-10-10T00:00:00Z",
        "url": "https://github.com/skyslope/agent-calculator/pull/1342",
    }
    if readable:
        pr["files_changed"] = [
            {"filename": AFFORDANCE_HOOK, "status": "modified", "patch": "+ label"},
        ]
    else:
        pr["github_enrichment_failed"] = True
    return [{"ticket_key": "SK-2579", "development_info": {"pull_requests": [pr]}}]


def _plan(*cases_by_section, regression=None) -> TestPlan:
    happy, edge = cases_by_section
    return TestPlan(
        happy_path=copy.deepcopy(happy),
        edge_cases=copy.deepcopy(edge),
        integration_tests=[],
        regression_checklist=list(regression or []),
    )


# ─── the failure, as it happened ──────────────────────────────────────────────


def test_the_connect_step_is_marked_unverified_not_just_the_expected_result():
    plan = _plan([], [EDGE_0])
    check_step_grounding(plan, readable_code_paths(_dev_info(readable=False)))
    case = plan.edge_cases[0]

    step6 = case["steps"][5]
    assert step6.startswith(MARKER)
    assert "“Connect”" in step6
    assert step6.endswith(EDGE_0["steps"][5])  # every word of the step kept
    assert case["preconditions"].startswith(MARKER)
    assert "“autoSync”" in case["preconditions"]
    # Steps that name nothing are left alone — a generic action is what a
    # plan should write when it cannot ground a label.
    assert case["steps"][:5] == EDGE_0["steps"][:5]
    assert case["steps"][6] == EDGE_0["steps"][6]

    assert {(r["step"], r["element"]) for r in case["unverified_steps"]} == {
        (0, "autoSync"),
        (6, "Connect"),
    }
    assert case["needs_manual_verification"] is True


def test_the_connect_case_leaves_the_gradeable_sections():
    """Neither the step nor the expected result traces to code, so the case
    is moved to needs_spec_cases instead of being graded — the outcome that
    would have kept a correct build from failing UAT."""
    plan = _plan([HAPPY_1], [EDGE_0])
    check_step_grounding(plan, readable_code_paths(_dev_info(readable=False)))
    moved = quarantine_ungrounded_cases(plan)

    assert [c["title"] for c in moved] == [EDGE_0["title"]]
    assert plan.edge_cases == []
    assert moved[0]["origin_section"] == "edge_cases"


def test_a_precondition_only_flag_warns_but_keeps_the_case_gradeable():
    """HAPPY_1's steps name nothing; only its precondition leans on the
    ticket's ``autoSync``. That is setup, not the behaviour under test, so the
    case stays where it is — marked and warned on."""
    plan = _plan([HAPPY_1], [])
    check_step_grounding(plan, readable_code_paths(_dev_info(readable=False)))
    quarantine_ungrounded_cases(plan)
    case = plan.happy_path[0]

    assert case["preconditions"].startswith(MARKER)
    assert case["steps"] == HAPPY_1["steps"]
    assert not case.get("needs_manual_verification")


def test_each_unbacked_reference_becomes_a_grounding_warning():
    plan = _plan([HAPPY_1], [EDGE_0])
    added = check_step_grounding(plan, readable_code_paths(_dev_info(readable=False)))
    rendered = normalize_grounding_warnings(plan)

    assert {(w["ac_id"], w["missing_element"]) for w in added} == {
        ("SK-2579-AC4", "autoSync"),
        ("SK-2579-AC6", "autoSync"),
        ("SK-2579-AC6", "Connect"),
    }
    connect = next(w for w in rendered if w["missing_element"] == "Connect")
    assert connect["severity"] == "warn"
    assert connect["source"] == "check_step_grounding"
    assert "step 6" in connect["explanation"]
    assert "ticket:" not in connect["missing_element"]


def test_marking_never_changes_how_many_steps_or_cases_there_are():
    plan = _plan([HAPPY_1], [EDGE_0], regression=[REGRESSION_6])
    before = fingerprint({
        "happy_path": plan.happy_path, "edge_cases": plan.edge_cases,
        "integration_tests": [], "regression_checklist": plan.regression_checklist,
    })
    check_step_grounding(plan, set())
    after = fingerprint({
        "happy_path": plan.happy_path, "edge_cases": plan.edge_cases,
        "integration_tests": [], "regression_checklist": plan.regression_checklist,
    })
    assert before == after
    assert len(plan.edge_cases[0]["steps"]) == len(EDGE_0["steps"])


def test_a_second_pass_does_not_stack_markers():
    plan = _plan([], [EDGE_0])
    check_step_grounding(plan, set())
    once = copy.deepcopy(plan.edge_cases[0])
    check_step_grounding(plan, set())
    assert plan.edge_cases[0]["steps"] == once["steps"]
    assert plan.edge_cases[0]["preconditions"] == once["preconditions"]


# ─── what does count as backing ───────────────────────────────────────────────


def test_a_reference_declared_against_a_readable_pr_file_is_backed():
    case = copy.deepcopy(EDGE_0)
    case["steps"][5] = "Tap Disconnect, then Connect, to link the calculation to the same Forms file again."
    case["preconditions"] = "A calculation linked to a Forms file from the Pilot mobile app."
    case["grounded_in"] = [f"pr:{AFFORDANCE_HOOK}"]
    case["step_grounding"] = [
        {"step": 6, "element": "Disconnect", "kind": "control", "source": f"pr:{AFFORDANCE_HOOK}"},
        {"step": 6, "element": "Connect", "kind": "control", "source": f"pr:{AFFORDANCE_HOOK}"},
    ]
    assert find_unbacked_references(case, readable_code_paths(_dev_info(readable=True))) == []


def test_a_pr_citation_from_a_pr_github_refused_does_not_count():
    """The model can cite a path it saw in a file list, or invent one. Either
    way, if the diff was not readable the citation cannot be a reading of it."""
    case = copy.deepcopy(EDGE_0)
    case["grounded_in"] = [f"pr:{AFFORDANCE_HOOK}"]
    case["step_grounding"] = [
        {"step": 6, "element": "Connect", "kind": "control", "source": f"pr:{AFFORDANCE_HOOK}"},
    ]
    refs = find_unbacked_references(case, readable_code_paths(_dev_info(readable=False)))
    connect = next(r for r in refs if r["element"] == "Connect")
    assert "not among the PR files that could be read" in connect["reason"]


def test_a_declared_reference_resting_on_the_ticket_is_unbacked_even_beside_code():
    """A case citing real code elsewhere does not launder a flag the ticket
    named: the declared source is what that reference rests on."""
    case = copy.deepcopy(HAPPY_1)
    case["grounded_in"] = ["ticket:SK-2579-desc", f"pr:{AFFORDANCE_HOOK}"]
    case["step_grounding"] = [
        {"step": 0, "element": "autoSync", "kind": "flag", "source": "ticket:SK-2579-desc"},
    ]
    refs = find_unbacked_references(case, readable_code_paths(_dev_info(readable=True)))
    assert [(r["step"], r["element"]) for r in refs] == [(0, "autoSync")]
    assert "not code" in refs[0]["reason"]


def test_the_intent_rewrite_passes_with_no_code_at_all():
    """The rewrite the prompt asks for. It names no control, so it is
    gradeable on any build and there is nothing to flag."""
    case = copy.deepcopy(EDGE_0)
    case["preconditions"] = "A calculation linked to a Forms file from the Pilot mobile app, with the net sheet PDF present on the file."
    case["steps"][5] = "Re-establish the link between the same calculation and the same Forms file, then change an input and wait for the sync window."
    assert find_unbacked_references(case, set()) == []


@pytest.mark.parametrize("text,element", [
    ("Link it again using Connect.", "Connect"),
    ("Tap 'Leave PDF' in the unlink dialog.", "Leave PDF"),
    ("With formsDocument.autoSync on.", "formsDocument.autoSync"),
    ("Choose the Leave PDF option.", "Leave PDF"),
])
def test_named_references_the_backstop_finds(text, element):
    assert element in [r["element"] for r in extract_named_references(text)]


@pytest.mark.parametrize("text", [
    "In Forms, delete the net sheet document from the file.",
    "In the mobile app, change a calculation input and let it save.",
    "Force-close the app on iOS and reopen it.",
    "Refresh the Forms file.",
])
def test_generic_actions_are_not_named_references(text):
    assert extract_named_references(text) == []


# ─── regression_checklist:6 ───────────────────────────────────────────────────


def test_a_line_enumerating_ui_actions_is_checked():
    """No control noun, so it used to skip the regression critic entirely."""
    assert names_a_control(REGRESSION_6)


def test_an_unverifiable_action_line_is_marked_as_an_assumption():
    plan = _plan([], [], regression=[REGRESSION_6])
    apply_regression_verdicts(plan, {
        line_id(0): {"verdict": "unverifiable", "reason": "no files-ui source", "rewrite": ""},
    })
    line = plan.regression_checklist[0]
    assert line.startswith("🔴 " + MARKER)
    assert line.endswith(REGRESSION_6[len("🔴 "):])


# ─── the generator is told ────────────────────────────────────────────────────


def test_the_prompt_carries_the_step_grounding_rules():
    for rule in (
        "A control must exist in the state your step is in",
        "Flags and settings are code too",
        "When code does not back it, write the intent",
        "An unverified expected result does not make its steps verified",
        "step_grounding",
    ):
        assert rule in UI_GROUNDING_GUIDANCE
    # Ticket text no longer backs a named control.
    assert "The ticket description or attached screenshots" not in UI_GROUNDING_GUIDANCE
    assert "renamed, downloaded and deleted" in SYSTEM_PROMPT


def test_the_schema_asks_for_step_grounding():
    prop = TEST_CASE_SCHEMA["properties"]["step_grounding"]
    item = prop["items"]
    assert item["required"] == ["step", "element", "kind", "source"]
    assert set(item["properties"]["kind"]["enum"]) == {"control", "action", "route", "flag"}


# ─── through the real pipeline ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_single_quarantines_the_connect_case_and_warns():
    """End to end through plan_service: the check runs after the critics and
    before quarantine, on the PR state the run actually had."""
    plan = _plan([HAPPY_1], [EDGE_0], regression=[REGRESSION_6])

    class _LLM:
        async def generate_test_plan(self, **kwargs):
            return plan

    async def _noop(*a, **k):
        return None

    request = GenerateTestPlanRequest(
        ticket_key="SK-2579",
        summary="Auto-sync the Forms net sheet PDF when a linked calculation changes",
        description="Acceptance Criteria:\n1. The PDF is replaced automatically\n",
        issue_type="Story",
        development_info=_dev_info(readable=False)[0]["development_info"],
    )
    with (
        patch.object(plan_service, "get_llm_client", return_value=_LLM()),
        patch.object(plan_service.JiraClient, "download_image_as_base64", AsyncMock(return_value=None)),
        patch.object(plan_service, "classify_deliverable", _noop),
        patch.object(plan_service, "run_grounding_critic", _noop),
        patch.object(plan_service, "run_code_grounding_critic", _noop),
        patch.object(plan_service, "run_fix_scope_critic", _noop),
        patch.object(plan_service, "run_surface_mismatch_critic", _noop),
        patch.object(plan_service, "run_regression_grounding_critic", _noop),
    ):
        response = await plan_service.generate_single(request)

    assert [c["title"] for c in response["needs_spec_cases"]] == [EDGE_0["title"]]
    assert [c["title"] for c in response["happy_path"]] == [HAPPY_1["title"]]
    assert response["edge_cases"] == []
    assert "Connect" in {w["missing_element"] for w in response["grounding_warnings"]}
    assert response["source_provenance"]["unbacked_step_references"] == 3
