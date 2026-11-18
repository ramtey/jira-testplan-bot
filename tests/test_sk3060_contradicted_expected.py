"""SK-3060 (plan 721 v1): a rule Q correction must replace the claim it contradicts.

The re-check found that county 'Notacounty' fails in county resolution — not a
SOAP call — before any later step sends a request, then prepended that to the
Expected and kept "The failed step's request XML is still in the ZIP" after it.
A tester could not grade it: whatever they saw, part of the Expected was false.
The steps also never reached what the title is about (a SOAP step that fails
after sending). Fixture: tests/fixtures/sk3060 (sanitized excerpts).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.app import plan_recheck as pr
from src.app.case_disposition import route_out_of_uat
from src.app.source_facts import SourceFile, SourceIndex

FIX = Path(__file__).parent / "fixtures" / "sk3060"
DATA = json.loads((FIX / "plan_721_edge_case_0.json").read_text())
REPO = "acme/agent-calculator"
BASE = "apps/server/src/services/gfe-service/"
FLOW, DEBUG, ZIP = BASE + "gfe-quote-flow.ts", BASE + "gfe-debug-service.ts", BASE + "gfe-debug-zip.ts"
DEV = [{"pull_requests": [{"repository": REPO, "number": 3060, "head_sha": "c" * 40,
                           "files_changed": [{"filename": DEBUG, "patch": "+ redactToken(requestXml)"}]}]}]

CONTRADICTED = "The failed step's request XML is still in the ZIP"
TITLE = "A GFE step that fails still produces redacted request XML in the debug ZIP"
STEP2 = "step2_GetCustomerConfiguredPropertyStates"


def _src(name: str) -> str:
    return (FIX / "src" / name).read_text()


def _index() -> SourceIndex:
    idx = SourceIndex()
    for path, name in ((FLOW, "gfe-quote-flow.ts"), (DEBUG, "gfe-debug-service.ts"), (ZIP, "gfe-debug-zip.ts")):
        idx.files[(REPO, path)] = SourceFile(REPO, path, "c" * 40, _src(name))
    return idx


def _plan(**sections):
    base = {s: [] for s in ("happy_path", "edge_cases", "integration_tests", "security_negative_tests",
                            "regression_checklist")}
    base.update(sections)
    return SimpleNamespace(**base, shared_preconditions=[])


def _fresh_case() -> dict:
    """The case as the generator wrote it, before any re-check."""
    c = copy.deepcopy(DATA["case"])
    c["expected"] = DATA["original_expected"]
    c.pop("trace_check")
    return c


class _FakeLLM:
    model = "m"

    def __init__(self, turns):
        self.turns = list(turns)

    def _budget(self, *a, **k):
        return 1000

    def _effort_kwargs(self, *_a):
        return {}

    async def _post_messages(self, body, **_kw):
        return self.turns.pop(0)


class _GH:
    async def fetch_files_at_ref(self, repo, ref, paths, **_):
        names = {FLOW: "gfe-quote-flow.ts", DEBUG: "gfe-debug-service.ts", ZIP: "gfe-debug-zip.ts"}
        return {p: _src(names[p]) for p in paths if p in names}


def _turns(verdict):
    reads = {"content": [{"type": "tool_use", "id": f"r{i}", "name": "read_file",
                          "input": {"repo": REPO, "path": p}} for i, p in enumerate((FLOW, DEBUG))]}
    return [reads, {"content": [{"type": "tool_use", "id": "x", "name": "report_recheck",
                                 "input": {"verdicts": [verdict]}}]}]


CORRECTED = (f"HTTP 200. summary.hasErrors is true and errors holds one entry, step 'CountyResolution': County "
             f"\"Notacounty\" not found in FNTG's list for CA (gfe-quote-flow.ts:14-16). The ZIP holds only "
             f"step1_GetCustomerProductForQuote_request.xml, step1_GetCustomerProductForQuote_response.xml, "
             f"{STEP2}_request.xml, {STEP2}_response.xml and summary.txt; no later step sends a request.")
VERDICT = {
    "id": "edge_cases:0:q", "verdict": "contradicted",
    "traced_path": f"{FLOW}:13 if (!resolvedCounty) → {FLOW}:16 onError('CountyResolution') → return at :17, "
                   f"before getCustomerByID at :22",
    "actual": DATA["why_full"], "reason": "county resolution is not a SOAP step and fails before any later request",
    "corrected_expected": CORRECTED, "steps_reach_intent": False,
    "actual_title": "An unknown county stops the GFE debug run after step 2 and names CountyResolution in summary",
}
TRIGGER = {
    "steps": ["Call gfeDebug.generateDebugZip with a valid CA address while FNTG answers GetGFEQuote with "
              "HasErrors (as on integ when it returned 'Service unavailable')",
              "Open the ZIP and read the GetGFEQuote request XML"],
    "expected": "summary.hasErrors is true with step GetGFEQuote; the ZIP holds the GetGFEQuote request XML and "
                "its <AccessToken> is [REDACTED].",
    "traced_path": f"{DEBUG}:36 HasErrors → throw → {DEBUG}:43 catch → {DEBUG}:45 redactToken(requestXml) → "
                   f"{ZIP}:6 redactToken again",
}


def _assert_gradeable(exp: str):
    assert CONTRADICTED not in exp
    assert "Contradicted by the code" not in exp
    assert "(Original expected:" not in exp


# --- the re-check replaces, never prepends ----------------------------------

@pytest.mark.asyncio
async def test_no_reachable_trigger_keeps_the_outcome_and_lists_the_intent_for_code_review():
    plan = _plan(edge_cases=[_fresh_case()])
    report: dict = {}
    gap = await pr.run_trace_critic(_FakeLLM(_turns({**VERDICT, "unreachable_reason":
                                                     "a tester cannot make FNTG fail a SOAP step on demand; a unit "
                                                     "test of captureStep's catch at gfe-debug-service.ts:19 checks it"})),
                                    _GH(), plan, DEV, report)
    assert gap is None
    case, intent = plan.edge_cases
    exp = case["expected"]
    _assert_gradeable(exp)
    assert exp.startswith("Re-traced from the code (rule Q): HTTP 200.") and f"{STEP2}_request.xml" in exp
    # The unreached detail is marked unverified, never asserted; the title no longer claims it.
    assert "Unverified — these steps do not reach it (rule Q):" in exp
    assert case["title"] == VERDICT["actual_title"]
    # The old claim is only in the grounding notes.
    assert case["grounding_downgrades"][0]["original"] == DATA["original_expected"]
    assert case["trace_check"]["original_expected"] == DATA["original_expected"]
    # The intent goes to code review / unit test with the reason.
    assert intent["title"] == TITLE and "rule Q" in intent["not_gradeable"]["reason"]
    out = route_out_of_uat(plan, dev_infos=DEV)
    assert [e["title"] for e in out["code_review_checks"]] == [TITLE]
    assert "captureStep" in out["code_review_checks"][0]["suggested_test"]
    assert [c["title"] for c in plan.edge_cases] == [VERDICT["actual_title"]]


@pytest.mark.asyncio
async def test_a_traced_trigger_refits_the_case_to_a_step_that_fails_after_sending_and_splits_the_rest():
    plan = _plan(edge_cases=[_fresh_case()])
    report: dict = {}
    await pr.run_trace_critic(_FakeLLM(_turns({**VERDICT, "reachable_trigger": TRIGGER})), _GH(), plan, DEV, report)
    intent, as_written = plan.edge_cases
    # The case keeps its title and now drives a SOAP step that fails after sending.
    assert intent["title"] == TITLE and "GetGFEQuote" in intent["steps"][0]
    assert "Notacounty" not in " ".join(intent["steps"])
    assert intent["expected"].endswith("its <AccessToken> is [REDACTED].") and DEBUG in intent["traced_path"]
    _assert_gradeable(intent["expected"])
    # What the original steps really produce is its own case, with the corrected Expected only.
    assert as_written["title"] == VERDICT["actual_title"] and as_written["split_from"] == TITLE
    assert "Notacounty" in as_written["steps"][0]
    _assert_gradeable(as_written["expected"])
    assert "CountyResolution" in as_written["expected"]
    # The save-time pass leaves both alone (no stale sentence, whole names).
    pr.recheck_before_save(plan, report, index=_index())
    assert [c["expected"] for c in plan.edge_cases] == [intent["expected"], as_written["expected"]]


@pytest.mark.asyncio
async def test_an_untraced_trigger_is_not_used():
    bad = {**TRIGGER, "traced_path": "the debug service probably keeps it"}
    plan = _plan(edge_cases=[_fresh_case()])
    await pr.run_trace_critic(_FakeLLM(_turns({**VERDICT, "reachable_trigger": bad})), _GH(), plan, DEV, {})
    assert plan.edge_cases[0]["title"] == VERDICT["actual_title"]
    assert plan.edge_cases[1].get("not_gradeable")


@pytest.mark.asyncio
async def test_the_critics_text_is_never_cut_into_the_expected():
    v = {k: x for k, x in VERDICT.items() if k not in ("corrected_expected", "steps_reach_intent")}
    plan = _plan(edge_cases=[_fresh_case()])
    await pr.run_trace_critic(_FakeLLM(_turns(v)), _GH(), plan, DEV, {})
    exp = plan.edge_cases[0]["expected"]
    _assert_gradeable(exp)
    assert f"{STEP2} request" in exp and plan.edge_cases[0]["title"] == TITLE


# --- the save-time check rejects what slipped through ---------------------------

def test_the_published_plan_721_expected_is_cleaned_at_save():
    plan = _plan(edge_cases=[copy.deepcopy(DATA["case"])])
    report: dict = {}
    pr.recheck_before_save(plan, report, index=_index())
    case = plan.edge_cases[0]
    exp = case["expected"]
    _assert_gradeable(exp)
    assert "Re-traced from the code (rule Q): hasErrors is true" in exp
    # The cut file name is restored from the code.
    assert f"{STEP2}" in exp and "GetCustomerConfiguredProper " not in exp + " "
    assert [g["original"] for g in case["grounding_downgrades"] if "original" in g] == [DATA["original_expected"]]
    rules = [(f["rule"], f["action"]) for f in report["findings"]]
    assert ("Q", "downgraded") in rules and ("Q", "fixed") in rules


def test_a_generator_echo_of_the_old_marker_is_cleaned_too():
    """A regeneration prompt shows the previous plan; a copied marker has no trace_check."""
    case = {"title": "Offline save", "steps": ["Go offline", "Save"],
            "expected": "Contradicted by the code (rule Q): an error alert appears (Save.kt:40) — "
                        "The draft is kept and a blank screen shows."}
    plan = _plan(edge_cases=[case])
    assert pr.check_contradicted_expected(plan, {}) == 1
    assert case["expected"] == "Re-traced from the code (rule Q): an error alert appears (Save.kt:40)"
    assert case["grounding_downgrades"][-1]["original"] == "The draft is kept and a blank screen shows."


def test_a_contradicted_sentence_that_comes_back_after_the_rewrite_is_removed():
    case = {"title": "t", "steps": ["s"], "expected": f"Re-traced from the code (rule Q): {CORRECTED} {CONTRADICTED}.",
            "trace_check": {"verdict": "contradicted", "corrected_expected": CORRECTED,
                            "original_expected": DATA["original_expected"]}}
    plan = _plan(edge_cases=[case])
    assert pr.check_contradicted_expected(plan, {}) == 1
    assert case["expected"] == f"Re-traced from the code (rule Q): {CORRECTED}"
    # A sentence the correction itself keeps is not "stale".
    assert pr.check_contradicted_expected(plan, {}) == 0


def test_names_quoted_whole_or_unread_are_left_alone():
    case = {"title": "t", "steps": ["s"],
            "expected": f"The ZIP holds {STEP2}_request.xml and step9_SomeUnreadServiceCall_request.xml."}
    plan = _plan(edge_cases=[case])
    report: dict = {}
    assert pr.check_quoted_names(plan, report, _index()) == 0
    assert case["expected"].startswith(f"The ZIP holds {STEP2}_request.xml")
    # Absent from the PRs' files is "could not check", never "not in the code".
    assert any("SomeUnreadServiceCall" in u for u in report["unchecked"])
