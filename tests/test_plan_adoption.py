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
    parse_plan_from_parts,
    select_comment,
    select_comments,
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


# --- Plans split across several Jira comments -----------------------------
#
# A plan too large for one comment is posted as "(part N of M)" comments, each
# carrying the marker. Adoption used to take `plan_comments[-1]` — the last
# part — and reconstruct a fraction of the plan from it. That is worse than
# failing: it parses cleanly, derives a plausible key for the fragment, and
# strands every mark under a key the UI never polls. SK-2246 is the real case:
# a 29-case plan over three comments parsed as 0-0-0-11.


def _split_adf(plan: dict, boundaries: list[str]) -> list[dict]:
    """Render `plan` and cut it into parts before each heading in `boundaries`,
    the way jira_client splits an oversized plan — the open section banner
    repeated with "(continued)" at the top of a part that starts mid-section."""
    full = _render_adf(plan)
    nodes = full["content"][1]["content"]

    cuts = [0]
    for node in nodes[1:]:
        text = node.get("attrs", {}).get("title") or (
            node.get("content", [{}])[0].get("text", "")
            if node.get("type") == "paragraph"
            else ""
        )
        if any(text.startswith(b) for b in boundaries):
            cuts.append(nodes.index(node))
    cuts.append(len(nodes))
    chunks = [nodes[a:b] for a, b in zip(cuts, cuts[1:]) if nodes[a:b]]

    total = len(chunks)
    return [
        {
            "type": "doc",
            "content": [
                _para(f"🤖 Generated Test Plan (part {i + 1} of {total})"),
                {"type": "expand", "attrs": {"title": "Click to view"}, "content": chunk},
            ],
        }
        for i, chunk in enumerate(chunks)
    ]


def _plan_29() -> dict:
    def cases(n, prefix):
        return [
            {"title": f"{prefix} case {i}", "steps": [f"Step {i}"], "expected": "It works"}
            for i in range(1, n + 1)
        ]

    return {
        "happy_path": cases(6, "Happy"),
        "edge_cases": cases(8, "Edge"),
        "integration_tests": cases(4, "Integration"),
        "regression_checklist": [f"Regression item {i}" for i in range(1, 12)],
    }


def _comment(cid: str, adf: dict) -> dict:
    return {"id": cid, "created": "2026-09-15T09:46:30.210-0700", "body": adf}


def test_every_part_of_a_split_plan_is_adopted_not_just_the_last():
    """The whole point: 6-8-4-11, not the 0-0-0-11 the last part alone gives."""
    plan = _plan_29()
    parts = _split_adf(plan, ["🔍 EDGE CASES", "🔗 INTEGRATION"])
    assert len(parts) > 1, "fixture did not actually split"

    comments = [_comment(f"33014{i}", adf) for i, adf in enumerate(parts)]
    chosen = select_comments(comments, None)
    assert len(chosen) == len(parts)

    parsed = parse_plan_from_parts([c["body"] for c in chosen], ticket_key="SK-2246")
    summary = summarize(parsed, ["SK-2246"])
    assert summary["section_counts"] == {
        "happy_path": 6, "edge_cases": 8, "integration_tests": 4,
        "regression_checklist": 11,
    }
    assert summary["case_count"] == 29


def test_a_split_plan_adopts_to_the_same_key_as_the_unsplit_one():
    """Splitting is a transport detail. If it moved the key, every mark written
    against the plan before the split would be orphaned after it."""
    plan = _plan_29()
    whole = parse_plan_from_adf(_render_adf(plan), ticket_key="SK-2246")
    parts = _split_adf(plan, ["🔍 EDGE CASES", "🔗 INTEGRATION"])
    split = parse_plan_from_parts([p for p in parts], ticket_key="SK-2246")

    assert build_progress_key(["SK-2246"], json.dumps(split)) == build_progress_key(
        ["SK-2246"], json.dumps(whole)
    )


def test_a_continued_section_banner_files_its_cases_under_the_right_section():
    """A part that opens mid-section repeats its banner as "… (continued)".
    Unmatched, its cases land before any heading and are dropped as headless."""
    plan = _plan_29()
    full = _render_adf(plan)
    nodes = full["content"][1]["content"]
    cut = next(i for i, n in enumerate(nodes) if (n.get("attrs", {}).get("title") or "").startswith("3."))

    part1 = {"type": "doc", "content": [
        _para("🤖 Generated Test Plan (part 1 of 2)"),
        {"type": "expand", "attrs": {"title": "Click to view"}, "content": nodes[:cut]}]}
    part2 = {"type": "doc", "content": [
        _para("🤖 Generated Test Plan (part 2 of 2)"),
        {"type": "expand", "attrs": {"title": "Click to view"},
         "content": [_para("✅ HAPPY PATH TEST CASES (continued)"), *nodes[cut:]]}]}

    parsed = parse_plan_from_parts([part1, part2], ticket_key="SK-2246")
    assert parsed["adoption_warnings"] == []
    assert len(parsed["happy_path"]) == 6


def test_a_regression_checklist_split_across_parts_keeps_both_halves():
    """The checklist banner appears in each part that carries it; assigning
    instead of extending would keep only the last part's bullets."""
    def checklist(items):
        return {"type": "nestedExpand", "attrs": {"title": "🔄 REGRESSION CHECKLIST"},
                "content": [_para(f"• {i}") for i in items]}

    part1 = {"type": "doc", "content": [
        _para("🤖 Generated Test Plan (part 1 of 2)"),
        {"type": "expand", "attrs": {"title": "Click to view"}, "content": [
            _para("✅ HAPPY PATH TEST CASES"),
            _case_expand(1, {"title": "Only case", "steps": ["Do it"]}, with_category=False),
            checklist(["A", "B"])]}]}
    part2 = {"type": "doc", "content": [
        _para("🤖 Generated Test Plan (part 2 of 2)"),
        {"type": "expand", "attrs": {"title": "Click to view"},
         "content": [checklist(["C", "D"])]}]}

    parsed = parse_plan_from_parts([part1, part2], ticket_key="SK-2246")
    assert parsed["regression_checklist"] == ["A", "B", "C", "D"]


def test_a_split_plan_missing_a_part_is_refused_rather_than_adopted_short():
    """A hole gives wrong counts, and wrong counts are the whole failure mode.
    Better to refuse than to persist a key nothing will poll."""
    plan = _plan_29()
    parts = _split_adf(plan, ["🔍 EDGE CASES", "🔗 INTEGRATION"])
    comments = [_comment(f"33014{i}", adf) for i, adf in enumerate(parts)]
    del comments[1]

    with pytest.raises(PlanAdoptionError, match="missing"):
        select_comments(comments, None)


def test_naming_one_part_adopts_the_whole_plan_it_belongs_to():
    """A comment_id pointing at part 3 means "adopt this plan", not "adopt this
    third of it" — the id is how a human refers to the plan they can see."""
    plan = _plan_29()
    parts = _split_adf(plan, ["🔍 EDGE CASES", "🔗 INTEGRATION"])
    comments = [_comment(f"33014{i}", adf) for i, adf in enumerate(parts)]

    chosen = select_comments(comments, comments[-1]["id"])
    assert [c["id"] for c in chosen] == [c["id"] for c in comments]


def test_a_single_comment_plan_still_adopts_as_one():
    """The ordinary case keeps its old behaviour — one comment, no part header."""
    comments = [_comment("1", _render_adf(_plan_29()))]
    assert select_comments(comments, None) == comments
    assert select_comment(comments, None) is comments[0]
