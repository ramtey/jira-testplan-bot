"""Rules L and M: a branch nothing reaches is unit coverage, and a CI claim names its file."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.app import evidence_checks as ev
from src.app import reachability_checks as rc

TEST_FILE = "apps/web/src/lib/parse-date.test.ts"
TEST_SRC = """import { parseDate } from "./parse-date";

describe("parseDate", () => {
  it("accepts the legacy form", () => {
    expect(parseDate("03/04/2025")).toEqual(new Date(2025, 2, 4));
  });
});
"""
REPO_EVIDENCE = [{
    "repo": "acme/web", "ref": "abc1234", "error": None,
    "test_sources": {TEST_FILE: TEST_SRC}, "truncated": [],
    "build": {"suites": {TEST_FILE: {"status": "runs",
                                     "why": ".github/workflows/ci.yml runs `pnpm exec turbo run test`"}},
              "findings": []},
}]
PRS = [{"repository": "acme/web", "number": 7, "title": "parse dates", "head_sha": "abc1234",
        "files_changed": [{"filename": "apps/web/src/lib/parse-date.ts", "patch": "@@\n+legacy"}]}]


def _ui_case(title="Legacy date is parsed on the invoice form", **kw):
    return {"title": title, "priority": "high", "surface": "web_ui", "credentials": "test user",
            "preconditions": "Signed in on integ", "unit_test_gap": "the test does not drive the UI",
            "steps": ["Open the invoice form", "Type 03/04/2025"], "expected": "The date shows as 4 March 2025",
            "expected_verified": True, "trigger": "Type a legacy date in the form",
            "environment": "integ", "needs_manual_verification": True,
            "step_grounding": [{"step": 1, "element": "invoice form", "kind": "route", "source": ""}], **kw}


def _plan(**sections):
    base = {"happy_path": [], "edge_cases": [], "integration_tests": [], "security_negative_tests": [],
            "regression_checklist": [], "risks_and_gaps": []}
    base.update(sections)
    return SimpleNamespace(**base)


UNREACHABLE_WITH_TEST = {
    "id": "happy_path:0", "verdict": "unreachable",
    "reason": "every caller formats dates as ISO (src/api/invoices.ts:40); the form has a date picker",
    "unit_test_ref": f"{TEST_FILE} › accepts the legacy form",
    "coverage_evidence": 'expect(parseDate("03/04/2025")).toEqual(new Date(2025, 2, 4));',
    "unit_title": "parseDate accepts the legacy MM/DD/YYYY form",
    "unit_expected": "parseDate returns 4 March 2025 for 03/04/2025",
    "unit_steps": [f"Run vitest on {TEST_FILE}"],
}


# --- CI detection (the root of the false "no CI runs a test command") -------

@pytest.mark.parametrize("line", [
    'pnpm exec turbo run test "${args[@]}"',
    "pnpm --filter @acme/web test",
    "yarn workspace web test",
    "nx run-many -t test",
    "npx playwright test",
    "pnpm test:integration",
])
def test_monorepo_test_commands_count_as_ci(line):
    tree = [".github/workflows/ci.yml", TEST_FILE]
    files = {".github/workflows/ci.yml": f"jobs:\n  t:\n    steps:\n      - run: {line}\n"}
    build = ev.analyze_build(tree, files, ["apps/web/src/lib/parse-date.ts"], [TEST_FILE])
    assert build["suites"][TEST_FILE]["status"] == "runs"


@pytest.mark.parametrize("line", ["pnpm --filter @acme/test-utils build", "runs-on: ubuntu-latest", "pnpm lint"])
def test_lines_that_only_look_like_tests_do_not_count(line):
    assert not ev._TEST_CMD_RE.search(line)


def test_an_unrecognised_test_step_is_unknown_not_absent():
    """A pattern miss is not a fact about CI."""
    tree = [".github/workflows/ci.yml", TEST_FILE]
    files = {".github/workflows/ci.yml": "jobs:\n  test:\n    steps:\n      - run: ./scripts/check-all.sh\n"}
    build = ev.analyze_build(tree, files, ["apps/web/src/lib/parse-date.ts"], [TEST_FILE])
    assert build["suites"][TEST_FILE]["status"] == "unknown"
    assert ".github/workflows/ci.yml" in build["suites"][TEST_FILE]["why"]
    assert not any(f["kind"] == "tests_not_run_in_ci" for f in build["findings"])


def test_not_run_names_the_ci_files_it_read():
    tree = [".github/workflows/lint.yml", TEST_FILE]
    files = {".github/workflows/lint.yml": "jobs:\n  lint:\n    steps:\n      - run: pnpm lint\n"}
    build = ev.analyze_build(tree, files, ["apps/web/src/lib/parse-date.ts"], [TEST_FILE])
    assert build["suites"][TEST_FILE]["status"] == "not_run"
    assert ".github/workflows/lint.yml" in build["suites"][TEST_FILE]["why"]


# --- L: applying the critic's verdicts --------------------------------------

def test_an_unreachable_case_with_a_test_becomes_unit_coverage_in_place():
    plan = _plan(happy_path=[_ui_case(), _ui_case("Other")])
    items = rc.collect_reachability_items(plan)
    out = rc.apply_reachability_verdicts(plan, items, {"happy_path:0": UNREACHABLE_WITH_TEST})
    assert out["converted"] == ["happy_path:0"]
    case = plan.happy_path[0]
    assert len(plan.happy_path) == 2, "a conversion never moves a case"
    assert case["covered_by_unit_test"] is True and case["surface"] == "cli"
    assert case["title"] == "parseDate accepts the legacy MM/DD/YYYY form"
    assert case["reachability"]["original_title"] == "Legacy date is parsed on the invoice form"
    for gone in ("trigger", "needs_manual_verification", "step_grounding", "unit_test_gap"):
        assert gone not in case
    assert not ev.needs_deployed_env(case), "a unit test needs no deploy check"
    assert plan.risks_and_gaps == []


def test_a_converted_case_survives_the_rule_c_d_check():
    plan = _plan(happy_path=[_ui_case()])
    rc.apply_reachability_verdicts(plan, rc.collect_reachability_items(plan),
                                   {"happy_path:0": UNREACHABLE_WITH_TEST})
    demoted, surviving = ev.enforce_unit_test_coverage(
        plan, REPO_EVIDENCE[0]["test_sources"], REPO_EVIDENCE[0]["build"]["suites"])
    assert demoted == [] and [s["case_id"] for s in surviving] == ["happy_path:0"]


def test_a_passed_case_is_marked_never_rewritten():
    plan = _plan(happy_path=[_ui_case()])
    out = rc.apply_reachability_verdicts(
        plan, rc.collect_reachability_items(plan), {"happy_path:0": UNREACHABLE_WITH_TEST},
        protected_titles=frozenset({"Legacy date is parsed on the invoice form"}))
    case = plan.happy_path[0]
    assert out["marked"] == ["happy_path:0"] and not case.get("covered_by_unit_test")
    assert case["title"] == "Legacy date is parsed on the invoice form"
    assert case["expected"].startswith(rc.UNREACHABLE_MARKER)
    assert case["reachability"]["unit_test_ref"].startswith(TEST_FILE)


def test_an_unreachable_case_without_a_test_is_marked_and_gets_a_follow_up():
    plan = _plan(edge_cases=[_ui_case()])
    verdict = {"id": "edge_cases:0", "verdict": "unreachable", "reason": "nothing sends that form"}
    rc.apply_reachability_verdicts(plan, rc.collect_reachability_items(plan), {"edge_cases:0": verdict},
                                   pull_requests=PRS)
    assert plan.edge_cases[0]["expected"].startswith(rc.UNREACHABLE_MARKER)
    [gap] = plan.risks_and_gaps
    assert gap["follow_up"] is True and "acme/web" in gap["owner"] and gap["suggested_ticket"]


def test_unsure_changes_nothing_and_reachable_fills_a_missing_trigger():
    a, b = _ui_case(trigger=""), _ui_case("B")
    before = dict(b)
    plan = _plan(happy_path=[a, b])
    rc.apply_reachability_verdicts(plan, rc.collect_reachability_items(plan), {
        "happy_path:0": {"id": "happy_path:0", "verdict": "reachable", "reason": "r",
                         "trigger": "Submit the form (src/ui/Form.tsx:8)"},
        "happy_path:1": {"id": "happy_path:1", "verdict": "unsure", "reason": "could not tell"},
    })
    assert plan.happy_path[0]["trigger"] == "Submit the form (src/ui/Form.tsx:8)"
    assert plan.happy_path[1] == before


def test_only_ui_cases_the_tester_performs_are_checked():
    plan = _plan(integration_tests=[{**_ui_case(), "surface": "backend_http"}],
                 edge_cases=[{**_ui_case(), "covered_by_unit_test": True}])
    assert rc.collect_reachability_items(plan) == []


class _FakeLLM:
    model = "m"

    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, 0

    def _budget(self, *a, **k):
        return 1000

    def _effort_kwargs(self, *_a):
        return {}

    async def _post_messages(self, body, **_kw):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


@pytest.mark.asyncio
async def test_the_critic_reports_through_its_tool():
    llm = _FakeLLM({"content": [{"type": "tool_use", "id": "t1", "name": "report_reachability",
                                 "input": {"verdicts": [UNREACHABLE_WITH_TEST]}}]})
    plan = _plan(happy_path=[_ui_case()])
    changes, gap = await rc.run_reachability_critic(llm, None, plan, [{"pull_requests": PRS}],
                                                    repo_evidence=REPO_EVIDENCE)
    assert gap is None and changes["converted"] == ["happy_path:0"]


@pytest.mark.asyncio
async def test_a_critic_that_could_not_run_says_so():
    plan = _plan(happy_path=[_ui_case()])
    changes, gap = await rc.run_reachability_critic(_FakeLLM(error=RuntimeError("down")), None, plan,
                                                    [{"pull_requests": PRS}])
    assert "could not run" in gap and changes["converted"] == []
    assert plan.happy_path[0]["title"] == "Legacy date is parsed on the invoice form"


def test_the_message_carries_cases_diffs_tests_and_ci_status():
    plan = _plan(happy_path=[_ui_case()])
    msg = rc.build_reachability_message(rc.collect_reachability_items(plan), PRS, REPO_EVIDENCE, None)
    assert "Trigger (as the plan states it): Type a legacy date" in msg
    assert "acme/web:apps/web/src/lib/parse-date.ts" in msg
    assert f"TEST FILE {TEST_FILE} — CI status: RUNS" in msg


# --- M: CI claims ------------------------------------------------------------

def _cli_case(expected, **kw):
    return {"title": "Run the suite", "surface": "cli", "steps": ["run it"], "expected": expected, **kw}


def test_an_uncited_ci_claim_is_marked_unverified():
    plan = _plan(integration_tests=[_cli_case("No CI configuration in the repo runs a test command.")])
    flagged = rc.flag_ci_claims(plan, [])
    assert flagged == [{"location": "integration_tests:0", "kind": "uncited"}]
    assert plan.integration_tests[0]["expected"].startswith(rc.CI_UNCITED_MARKER)


def test_a_cited_ci_claim_is_left_alone():
    plan = _plan(integration_tests=[_cli_case(
        "CI runs this file: .github/workflows/ci.yml job `test` runs `pnpm exec turbo run test`.")])
    assert rc.flag_ci_claims(plan, []) == []


def test_saying_ci_does_not_run_a_file_it_runs_is_contradicted():
    plan = _plan(integration_tests=[_cli_case(f"{TEST_FILE} is not run by CI, so run it locally.")],
                 risks_and_gaps=[{"gap": f"{TEST_FILE} is not executed by any CI configuration"}])
    flagged = rc.flag_ci_claims(plan, REPO_EVIDENCE)
    assert {f["kind"] for f in flagged} == {"contradicted"}
    assert "Contradicted by the CI config — .github/workflows/ci.yml runs" in plan.integration_tests[0]["expected"]
    assert plan.risks_and_gaps[0]["gap"].startswith("Contradicted by the CI config")


def test_naming_ci_as_a_platform_is_not_a_ci_claim():
    plan = _plan(edge_cases=[_cli_case("On macOS it passes; on Linux CI it exits 1.")],
                 regression_checklist=["🔴 The script exits 1 on Linux CI"])
    assert rc.flag_ci_claims(plan, []) == []


def test_a_regression_line_keeps_its_priority_emoji_first():
    plan = _plan(regression_checklist=["🟡 CI does not run the web tests"])
    rc.flag_ci_claims(plan, [])
    assert plan.regression_checklist[0].startswith("🟡 " + rc.CI_UNCITED_MARKER)


def test_asking_to_check_the_ci_scope_is_not_a_negative_claim():
    """SK-2947 regeneration: a case that says CI runs the file was marked contradicted."""
    text = ("Confirm the CI workflow's test scope includes this file: verify apps/web is within that turbo "
            "scope (not filtered out) by checking the workflow's filter arguments.")
    plan = _plan(integration_tests=[_cli_case(text + " CI runs it via .github/workflows/ci.yml.")])
    assert rc.flag_ci_claims(plan, REPO_EVIDENCE) == []


def test_a_parameterised_test_body_includes_its_table():
    src = ('describe("m", () => {\n  it.each(["Loading chunk 9 failed.", "Loading CSS chunk 12 failed."])(\n'
           '    "recognises %s when the name was lost",\n    (message) => {\n'
           '      expect(isChunkLoadError(new Error(message))).toBe(true);\n    },\n  );\n'
           '  it("other", () => { expect(1).toBe(1); });\n});\n')
    body = ev.extract_test_body(src, "recognises %s when the name was lost")
    assert "Loading CSS chunk 12 failed." in body and "expect(isChunkLoadError" in body
    assert "other" not in body
    assert ev.extract_test_body(src, "other").startswith('"other"'), "a plain it() is unchanged"


def test_a_true_uncited_ci_claim_gets_the_build_fact_attached():
    case = _cli_case("Covered by the unit suite, which CI runs.", unit_test_ref=f"{TEST_FILE} › accepts the legacy form")
    plan = _plan(edge_cases=[case])
    assert rc.flag_ci_claims(plan, REPO_EVIDENCE) == [{"location": "edge_cases:0", "kind": "cited_from_build_facts"}]
    assert case["expected"].startswith("Covered by the unit suite")
    assert "(CI evidence: .github/workflows/ci.yml runs" in case["expected"]
