"""Adopting a plan back out of its Jira comment must reproduce the same key.

``plan_adoption`` exists because a plan can reach a Jira ticket without ever
being persisted: ``start_run`` swallows a database error and returns
``RunContext(run_id=None)``, so no ``plan_id`` is produced and no run or plan
row is written. SK-2342 is the case — a transient Atlas outage on 2026-09-15,
comment 330141 on the ticket, nothing in the database. With no run and no plan
body there is no ``build_progress_key`` result to write UAT progress under.

The one thing adoption must not do is invent a second way to count a plan into
a key. ``progress_key``'s docstring records what that costs: SK-2642 was marked
under ``SK-2642:4-8-2-7`` while the UI polled ``SK-2642:4-8-1-7`` and rendered
0/20. So these tests pin the property that actually matters — a plan and the
comment it was rendered into produce the *same* progress key — rather than
merely asserting the parser returns something.

The fixture is the real comment 330141 with its ADF structure preserved
node-for-node and its prose replaced: this repository is public, and the
original carried an unreleased feature's details, a reporter's email and Jira
account id, and internal file paths. Structure is what these tests pin, so
nothing of test value was lost with it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.app.services.plan_adoption import (
    PlanAdoptionError,
    parse_plan_from_adf,
    select_comment,
    summarize,
)
from src.app.services.progress_key import build_progress_key

FIXTURE = Path(__file__).parent / "fixtures" / "sk2342_comment.json"


def _sk2342_adf() -> dict:
    return json.loads(FIXTURE.read_text())["comments"][0]["body"]


# --- Helpers that mirror how jira_client renders a plan into ADF ----------


def _para(text: str) -> dict:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def _case_expand(number: int, case: dict, *, with_category: bool) -> dict:
    """A case as ``_group_test_cases_into_nested_expands`` writes it."""
    title = f"{number}. {case['title']}"
    if case.get("priority"):
        emoji = {"critical": "🔴", "high": "🟡"}.get(case["priority"], "🟢")
        title += f" {emoji} {case['priority'].upper()}"
    if with_category and case.get("category"):
        title += f" [{case['category']}]"
    content = []
    if case.get("preconditions"):
        content.append(_para(f"Preconditions: {case['preconditions']}"))
    if case.get("steps"):
        content.append(_para("Steps:"))
        content.append(
            {
                "type": "orderedList",
                "content": [
                    {"type": "listItem", "content": [_para(s)]} for s in case["steps"]
                ],
            }
        )
    if case.get("expected"):
        content.append(_para(f"Expected Result: {case['expected']}"))
    return {"type": "nestedExpand", "attrs": {"title": title}, "content": content}


def _render_adf(plan: dict, *, include_covered: bool = False) -> dict:
    """Build the ADF a plan would be posted as.

    Mirrors ``formatTestPlanAsJira``: the four graded sections are rendered with
    cases flagged ``covered_by_unit_test`` filtered out, and those cases appear
    only in a trailing section, and only when the poster opted in.
    """
    nodes: list[dict] = [_para("🧭 HOW TO TEST THIS — START HERE")]
    covered: list[dict] = []

    def _visible(key: str) -> list[dict]:
        out = []
        for case in plan.get(key) or []:
            if case.get("covered_by_unit_test"):
                covered.append(case)
            else:
                out.append(case)
        return out

    for key, heading in (
        ("happy_path", "✅ HAPPY PATH TEST CASES"),
        ("edge_cases", "🔍 EDGE CASES & ERROR SCENARIOS"),
        ("integration_tests", "🔗 INTEGRATION & BACKEND TESTS"),
    ):
        cases = _visible(key)
        if not cases:
            continue
        nodes.append(_para(heading))
        for i, case in enumerate(cases, start=1):
            nodes.append(_case_expand(i, case, with_category=key == "edge_cases"))

    if plan.get("regression_checklist"):
        nodes.append(
            {
                "type": "nestedExpand",
                "attrs": {"title": "🔄 REGRESSION CHECKLIST"},
                "content": [_para(f"• {item}") for item in plan["regression_checklist"]],
            }
        )

    if include_covered and covered:
        nodes.append(_para(f"🧪 ALREADY COVERED BY UNIT TESTS ({len(covered)})"))

    return {
        "type": "doc",
        "content": [
            _para("🤖 Generated Test Plan"),
            {"type": "expand", "attrs": {"title": "Click to view"}, "content": nodes},
        ],
    }


# --- The real SK-2342 comment --------------------------------------------


def test_the_sk_2342_comment_yields_the_key_the_ui_will_poll():
    """The plan blocking the UAT run. 1 happy + 5 edge + 0 integration + 7
    regression = the 13 cases on the ticket."""
    plan = parse_plan_from_adf(_sk2342_adf(), ticket_key="SK-2342")
    summary = summarize(plan, ["SK-2342"])

    assert summary["section_counts"] == {
        "happy_path": 1,
        "edge_cases": 5,
        "integration_tests": 0,
        "regression_checklist": 7,
    }
    assert summary["case_count"] == 13
    assert summary["progress_key"] == "SK-2342:1-5-0-7"


def test_the_summary_key_is_not_computed_independently_of_the_stored_body():
    """``summarize`` must report exactly what ``/plans/{id}/progress-key`` will
    later derive from the persisted body — otherwise the preview could bless a
    key the adopted plan does not actually produce."""
    plan = parse_plan_from_adf(_sk2342_adf(), ticket_key="SK-2342")
    assert summarize(plan, ["SK-2342"])["progress_key"] == build_progress_key(
        ["SK-2342"], json.dumps(plan)
    )


def test_grounded_in_is_not_mistaken_for_the_integration_section():
    """'🔗 GROUNDED IN' shares its emoji with '🔗 INTEGRATION & BACKEND TESTS'.
    Matching on the prefix alone would file the PR provenance as a test section
    and shift the fingerprint."""
    plan = parse_plan_from_adf(_sk2342_adf(), ticket_key="SK-2342")
    assert plan["integration_tests"] == []


def test_the_regression_checklist_stops_at_the_next_section():
    """The ADF builder's ``_collect_until_next_section`` does not stop at '⚠️'
    or '🚧', so risks/gaps and needs-spec are swallowed into the regression
    nestedExpand. Counting them as regression items would inflate the last
    number of the fingerprint."""
    plan = parse_plan_from_adf(_sk2342_adf(), ticket_key="SK-2342")
    items = plan["regression_checklist"]

    assert len(items) == 7
    assert not any("RISKS / GAPS" in item for item in items)
    assert not any("NEEDS SPEC" in item for item in items)
    assert not any(item.startswith("Impact:") for item in items)


def test_needs_spec_cases_are_not_counted_as_test_cases():
    """SK-2342's comment carries 6 needs-spec entries. They are not gradeable
    and are not part of any counted section."""
    plan = parse_plan_from_adf(_sk2342_adf(), ticket_key="SK-2342")
    assert summarize(plan, ["SK-2342"])["case_count"] == 13


def test_case_details_survive_the_round_trip():
    plan = parse_plan_from_adf(_sk2342_adf(), ticket_key="SK-2342")
    case = plan["happy_path"][0]

    assert case["title"] == (
        "Recipient receives the default-language asset after the sender "
        "previews an alternate"
    )
    assert case["priority"] == "critical"
    assert "🔴" not in case["title"] and "CRITICAL" not in case["title"]
    assert len(case["steps"]) == 5
    assert case["preconditions"].startswith("Generic placeholder")
    assert case["expected_verified"] is False


def test_edge_case_categories_are_recovered_and_stripped_from_titles():
    plan = parse_plan_from_adf(_sk2342_adf(), ticket_key="SK-2342")
    assert [c.get("category") for c in plan["edge_cases"]] == [
        "error_handling",
        "error_handling",
        "error_handling",
        "boundary",
        "error_handling",
    ]
    assert not any("[" in c["title"] for c in plan["edge_cases"])


def test_an_adopted_plan_is_marked_as_adopted():
    """A reconstruction is missing metadata a generated plan carries. It must be
    distinguishable rather than passing as the real thing."""
    plan = parse_plan_from_adf(_sk2342_adf(), ticket_key="SK-2342")
    assert plan["adopted_from_jira"] is True


# --- The covered_by_unit_test rule ----------------------------------------


def test_a_covered_case_absent_from_the_comment_does_not_shift_the_key():
    """The rule that broke SK-2642, from the other direction.

    ``formatTestPlanAsJira`` renders the graded sections through ``uncovered()``,
    and ``includeCovered`` is off by default — so a case flagged
    ``covered_by_unit_test`` is simply not in the comment. That is exactly the
    case ``progress_key`` also declines to count, so the key adoption derives
    must equal the key the original plan produces.
    """
    original = {
        "happy_path": [{"title": "a"}, {"title": "b"}],
        "edge_cases": [{"title": "c"}],
        "integration_tests": [
            {"title": "d"},
            {"title": "unit-tested", "covered_by_unit_test": True},
        ],
        "regression_checklist": ["r1", "r2"],
    }
    adopted = parse_plan_from_adf(_render_adf(original), ticket_key="SK-1")

    assert build_progress_key(["SK-1"], json.dumps(adopted)) == build_progress_key(
        ["SK-1"], json.dumps(original)
    )
    assert summarize(adopted, ["SK-1"])["progress_key"] == "SK-1:2-1-1-2"


def test_the_covered_section_banner_is_never_counted_as_a_case():
    """With ``includeCovered`` on, the comment gains a '🧪 ALREADY COVERED'
    section. It is a record, not a checklist — counting it would reintroduce the
    SK-2642 split between what the runner marks and what the UI polls."""
    original = {
        "happy_path": [{"title": "a"}],
        "edge_cases": [{"title": "covered", "covered_by_unit_test": True}],
        "regression_checklist": ["r1"],
    }
    adopted = parse_plan_from_adf(
        _render_adf(original, include_covered=True), ticket_key="SK-1"
    )

    assert summarize(adopted, ["SK-1"])["progress_key"] == "SK-1:1-0-0-1"
    assert build_progress_key(["SK-1"], json.dumps(adopted)) == build_progress_key(
        ["SK-1"], json.dumps(original)
    )


# --- Refusals -------------------------------------------------------------


def test_a_comment_with_no_cases_is_refused_rather_than_adopted_empty():
    """An empty plan would satisfy the watcher's ``has_successful_test_plan``
    guard and retire the ticket from unattended generation while giving the
    tester nothing to test."""
    doc = {"type": "doc", "content": [_para("🤖 Generated Test Plan"), _para("hello")]}
    with pytest.raises(PlanAdoptionError):
        parse_plan_from_adf(doc, ticket_key="SK-1")


def test_a_non_plan_comment_is_flagged_even_when_it_parses():
    plan = {
        "happy_path": [{"title": "a"}],
        "regression_checklist": ["r"],
    }
    doc = _render_adf(plan)
    # Drop the bot marker, as a hand-written comment would lack it.
    doc["content"] = doc["content"][1:]
    parsed = parse_plan_from_adf(doc, ticket_key="SK-1")
    assert any("marker" in w for w in parsed["adoption_warnings"])


def test_selecting_a_missing_comment_id_is_an_error_not_a_silent_fallback():
    """Adopting the wrong comment would register a plan nobody reviewed."""
    with pytest.raises(PlanAdoptionError):
        select_comment([{"id": "1", "body": {}}], "330141")
