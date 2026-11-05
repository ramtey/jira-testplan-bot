"""Rules Q–Z, re-checked before a plan is published (src/app/plan_recheck.py).

Each test is one failure a UAT run found in a generated plan, written
generically (``acme`` placeholders): no ticket is special-cased.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.app import plan_recheck as pr
from src.app.case_disposition import CODE_REVIEW_KEY, PRE_EXISTING_KEY, route_out_of_uat
from src.app.source_facts import SourceFile, SourceIndex

REPO = "acme/docs-android"
CHANGED = "app/src/main/java/com/acme/docs/DocumentRepository.kt"


def _plan(**sections):
    base = {"happy_path": [], "edge_cases": [], "integration_tests": [], "regression_checklist": [],
            "security_negative_tests": []}
    return SimpleNamespace(**{**base, **sections})


def _case(**kw):
    return {"title": "Open a document", "steps": ["Open the document list", "Tap a document"],
            "expected": "The document opens.", "how_to_verify": "The viewer shows page 1.",
            "surface": "mobile", "expected_verified": True, "expected_source": f"{CHANGED}:40", **kw}


def _index(files: dict[str, str]):
    idx = SourceIndex()
    for path, text in files.items():
        idx.files[(REPO, path)] = SourceFile(REPO, path, "h" * 40, text)
    idx.refs[REPO] = "h" * 40
    idx.trees[REPO] = list(files)
    return idx


DEV = [{"pull_requests": [{"repository": REPO, "number": 7, "head_sha": "h" * 40,
                           "files_changed": [{"filename": CHANGED, "patch": "+ fun open(id: String): Doc?"}]}]}]


# --- S: "tests confirm" needs an assertion -----------------------------------

def test_a_test_claim_with_no_assertion_is_rewritten_as_read_from_code():
    plan = _plan(happy_path=[_case(expected="The upload runs before the cleanup. The unit tests confirm this order.")])
    report: dict = {}
    pr.check_test_claims(plan, report)
    exp = plan.happy_path[0]["expected"]
    assert f"No test asserts this (rule S) — from reading the code ({CHANGED}:40): The unit tests confirm" in exp
    assert exp.startswith("The upload runs before the cleanup."), "only the claim sentence is marked"
    assert report["findings"][0]["rule"] == "S"


def test_a_proven_coverage_claim_is_left_alone():
    case = _case(expected="Tests confirm the order.", covered_by_unit_test=True,
                 unit_test_ref="DocTest.kt › uploads before cleanup", coverage_evidence="verifyOrder { up(); clean() }")
    plan = _plan(happy_path=[case])
    pr.check_test_claims(plan, {})
    assert plan.happy_path[0]["expected"] == "Tests confirm the order."


# --- R: "reported" needs the reporting line ---------------------------------

def test_a_reported_claim_with_no_line_is_downgraded():
    plan = _plan(edge_cases=[_case(expected="The upload fails and the failure is reported to Sentry.")])
    report: dict = {}
    pr.check_signal_claims(plan, report)
    case = plan.edge_cases[0]
    assert pr.SIGNAL_MARKER in case["expected"] and case["expected_verified"] is False
    assert report["findings"][0]["rule"] == "R"


def test_a_cited_line_that_reports_keeps_the_claim():
    idx = _index({CHANGED: "a\nb\n    Sentry.captureException(e)\n"})
    plan = _plan(edge_cases=[_case(expected="The failure is reported to Sentry.",
                                   signal_sources=[{"claim": "reported", "line": f"{CHANGED}:3"}])])
    report: dict = {}
    pr.check_signal_claims(plan, report, idx)
    assert pr.SIGNAL_MARKER not in plan.edge_cases[0]["expected"] and not report.get("findings")


def test_a_cited_line_that_reports_nothing_is_a_downgrade():
    idx = _index({CHANGED: "val x = 1\nval y = 2\n"})
    plan = _plan(edge_cases=[_case(expected="The failure is logged.",
                                   signal_sources=[{"claim": "logged", "line": f"{CHANGED}:2"}])])
    report: dict = {}
    pr.check_signal_claims(plan, report, idx)
    assert any("does not report anything" in f["reason"] for f in report["findings"])
    assert pr.SIGNAL_MARKER in plan.edge_cases[0]["expected"]


# --- X: runnable and checkable ----------------------------------------------

def test_a_case_with_no_place_to_check_is_marked():
    plan = _plan(happy_path=[_case(how_to_verify="")])
    pr.check_runnable(plan, {})
    assert plan.happy_path[0]["expected"].startswith("Not runnable as written (rule X): no place to check")


def test_brief_ui_tells_the_tester_to_record():
    plan = _plan(happy_path=[_case(expected="A toast says the document was saved.")])
    report: dict = {}
    pr.check_runnable(plan, report)
    assert pr.BRIEF_UI_NOTE in plan.happy_path[0]["how_to_verify"]
    assert report["findings"][0]["action"] == "fixed"


def test_regression_lines_need_steps_expected_and_where_to_check():
    good = ("🔴 Sign out — at risk via DocumentRepository.kt. Steps: 1. Sign in 2. Sign out. "
            "Expected: the document list is empty. Check: the list screen.")
    bad = "🟡 Document list still loads — at risk via DocumentRepository.kt"
    plan = _plan(regression_checklist=[good, bad])
    pr.check_runnable(plan, {})
    assert plan.regression_checklist[0] == good
    assert plan.regression_checklist[1].startswith("🟡 Not runnable as written (rule X): no numbered steps")
    assert len(plan.regression_checklist) == 2, "a mark never changes the count (progress key)"


# --- Z: shared setup once ---------------------------------------------------

def test_setup_repeated_across_cases_is_written_once():
    pre = "Signed in as the agent test account with two documents uploaded."
    plan = _plan(happy_path=[_case(preconditions=pre), _case(title="b", preconditions=f"{pre} Offline mode on.")],
                 edge_cases=[_case(title="c", preconditions="Something else entirely, only here.")])
    report: dict = {}
    pr.hoist_shared_preconditions(plan, report)
    assert plan.shared_preconditions == [{"id": "S1", "text": pre}]
    assert plan.happy_path[0]["preconditions"] == "Precondition: S1 passed (top of plan)."
    assert plan.happy_path[1]["preconditions"] == "Precondition: S1 passed (top of plan). Offline mode on."
    assert plan.edge_cases[0]["preconditions"] == "Something else entirely, only here."


def test_the_build_check_precondition_is_never_hoisted_again():
    pre = "Precondition: Build check passed for docs-android (see Build check at the top of the plan)."
    plan = _plan(happy_path=[_case(preconditions=pre), _case(title="b", preconditions=pre)])
    pr.hoist_shared_preconditions(plan, {})
    assert not getattr(plan, "shared_preconditions", None)


# --- U: graded cases test touched code --------------------------------------

@pytest.mark.asyncio
async def test_a_case_the_plan_calls_unverified_against_shipped_code_leaves_the_graded_cases():
    plan = _plan(edge_cases=[_case(expected="Duplicate names get a suffix (unverified against shipped code).")])
    report: dict = {}
    await pr.check_graded_scope(plan, DEV, report)
    out = route_out_of_uat(plan, dev_infos=DEV)
    assert plan.edge_cases == [] and out[CODE_REVIEW_KEY][0]["why_not_uat"].startswith(
        "the plan itself marks this unverified against the shipped code")


class _GH:
    async def fetch_files_at_ref(self, repo, ref, paths, **_):
        return {}


@pytest.mark.asyncio
async def test_a_case_on_untouched_code_becomes_a_pre_existing_note():
    untouched = "app/src/main/java/com/acme/docs/SettingsScreen.kt"
    caller = "app/src/main/java/com/acme/docs/DocumentScreen.kt"
    idx = _index({CHANGED: "fun open() {}", untouched: "fun settings() { theme() }",
                  caller: "val doc = documentRepository.open(id) // DocumentRepository"})
    plan = _plan(happy_path=[_case(title="Theme toggles", expected_source=f"{untouched}:1"),
                             _case(title="Viewer opens", expected_source=f"{caller}:1")])
    report: dict = {}
    got = await pr.check_graded_scope(plan, DEV, report, index=idx, github=_GH())
    assert got["moved_pre_existing"] == ["Theme toggles"]
    out = route_out_of_uat(plan, dev_infos=DEV)
    assert [c["title"] for c in plan.happy_path] == ["Viewer opens"], "a direct caller stays graded"
    assert out[PRE_EXISTING_KEY][0]["title"] == "Theme toggles"


@pytest.mark.asyncio
async def test_a_cited_file_that_could_not_be_read_is_unchecked_not_moved():
    idx = _index({CHANGED: "x"})
    plan = _plan(happy_path=[_case(title="Somewhere else", expected_source="app/src/Other.kt:3")])
    report: dict = {}
    got = await pr.check_graded_scope(plan, DEV, report, index=idx, github=_GH())
    assert got["unchecked"] == ["Somewhere else"] and not plan.happy_path[0].get("pre_existing")


@pytest.mark.asyncio
async def test_a_case_qa_already_passed_is_never_moved():
    plan = _plan(edge_cases=[_case(title="Duplicate names get a suffix",
                                   expected="Unverified against shipped code.")])
    await pr.check_graded_scope(plan, DEV, {}, protected_titles=frozenset({"Duplicate names get a suffix"}))
    assert "not_gradeable" not in plan.edge_cases[0]


# --- Q/T: the traced path and the old code ----------------------------------

class _FakeLLM:
    model = "m"

    def __init__(self, turns):
        self.turns, self.calls = list(turns), 0

    def _budget(self, *a, **k):
        return 1000

    def _effort_kwargs(self, *_a):
        return {}

    async def _post_messages(self, body, **_kw):
        self.calls += 1
        return self.turns.pop(0)


class _ReadGH:
    async def fetch_files_at_ref(self, repo, ref, paths, **_):
        return {p: "val file = storage.open(id)\nrunCatching { parse(file) }" for p in paths}


def _report(verdict):
    return {"content": [{"type": "tool_use", "id": "r", "name": "report_recheck",
                         "input": {"verdicts": [verdict]}}]}


@pytest.mark.asyncio
async def test_an_expected_result_the_error_path_contradicts_is_downgraded():
    read = {"content": [{"type": "tool_use", "id": "a", "name": "read_file",
                         "input": {"repo": REPO, "path": CHANGED}}]}
    verdict = {"id": "edge_cases:0:q", "verdict": "contradicted",
               "traced_path": f"{CHANGED}:1 storage.open(id) is outside the runCatching at :2",
               "actual": f"storage.open throws before the runCatching ({CHANGED}:1), so the screen's error alert shows",
               "reason": "call outside runCatching"}
    plan = _plan(edge_cases=[_case(title="Missing file shows a blank screen",
                                   expected="A blank screen, because open() returns null on failure.")])
    report: dict = {}
    gap = await pr.run_trace_critic(_FakeLLM([read, _report(verdict)]), _ReadGH(), plan, DEV, report)
    case = plan.edge_cases[0]
    assert gap is None and case["expected"].startswith("Contradicted by the code (rule Q): storage.open throws")
    assert case["expected_verified"] is False and case["trace_check"]["verdict"] == "contradicted"
    assert report["findings"][0]["rule"] == "Q"


@pytest.mark.asyncio
async def test_a_contradiction_resting_on_an_unread_file_is_not_applied():
    verdict = {"id": "edge_cases:0:q", "verdict": "contradicted", "actual": "Other.kt:9 shows an alert",
               "reason": "guess"}
    plan = _plan(edge_cases=[_case(title="Missing file", expected="A blank screen on error.")])
    report: dict = {}
    await pr.run_trace_critic(_FakeLLM([_report(verdict)]), _ReadGH(), plan, DEV, report)
    assert plan.edge_cases[0]["expected"] == "A blank screen on error."
    assert report["trace"]["unverifiable"] == ["edge_cases:0:q"]


@pytest.mark.asyncio
async def test_before_claims_are_checked_at_the_ticket_base():
    read = {"content": [{"type": "tool_use", "id": "a", "name": "read_file",
                         "input": {"repo": REPO, "path": CHANGED, "at": "base"}}]}
    verdict = {"id": "happy_path:0:t", "verdict": "contradicted",
               "actual": f"the old code at {CHANGED}:1 named files by id, not by title", "reason": "old line"}
    llm = _FakeLLM([read, _report(verdict)])
    plan = _plan(happy_path=[_case(title="Download names the file", expected="Previously files were named by title.")])
    report: dict = {}
    await pr.run_trace_critic(llm, _ReadGH(), plan, DEV, report, bases={REPO: {"sha": "b" * 40, "pr": 7}})
    assert plan.happy_path[0]["expected"].startswith("Unverified before-behaviour (rule T)")


@pytest.mark.asyncio
async def test_a_critic_that_cannot_run_says_what_was_not_checked():
    class _Down(_FakeLLM):
        async def _post_messages(self, body, **_kw):
            raise RuntimeError("down")
    plan = _plan(edge_cases=[_case(expected="An error alert appears.")])
    gap = await pr.run_trace_critic(_Down([]), _ReadGH(), plan, DEV, {})
    assert "could not run" in gap and "not re-traced" in gap


# --- the prompt and schema carry the rules ----------------------------------

def test_the_prompt_and_schema_carry_rules_q_to_z():
    from src.app.llm_client import SYSTEM_PROMPT, TEST_CASE_SCHEMA

    for rule in ("**Q. Trace the path", "**R. A result only counts", "**S. Tests prove only",
                 "**T. \"Before this change\"", "**U. Graded cases test code", "**V. Cover what the change",
                 "**W. The test data must", "**X. Every case is runnable", "**Y. Use the repo's real",
                 "**Z. Write shared steps once", "TRACING SELF-CHECK (rules Q–Z)"):
        assert rule in SYSTEM_PROMPT, rule
    assert "how_to_verify" in TEST_CASE_SCHEMA["required"]
    for field in ("traced_path", "signal_sources", "before_change", "how_to_verify"):
        assert field in TEST_CASE_SCHEMA["properties"], field
