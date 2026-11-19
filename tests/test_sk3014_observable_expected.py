"""SK-3014 (plan 734 v1): an Expected asserts what a tester observes, never which internal path ran (rule AK).

edge_cases:4 ended "The RPC created the row first, and upsertStreamedText then
took the 23505 → update path with no error." upsertStreamedText reads the row
first (streamedTextsRepository.ts:114); the refusal RPC has already created it,
so it updates (:139-150). The 23505 fallback (:128-135) needs a concurrent
insert between the check and the insert, which one sequential message never
makes. Fixture: tests/fixtures/sk3014.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.app import observable_expected as ak
from src.app import plan_recheck as pr

FIX = Path(__file__).parent / "fixtures" / "sk3014"
DATA = json.loads((FIX / "plan_734_edge_case_4.json").read_text())
REPO = "acme/agent-coach"
REPO_FILE = "packages/supabase/src/public/repositories/streamedTextsRepository.ts"
SRC = (FIX / "src" / "streamedTextsRepository.ts").read_text()
DEV = [{"pull_requests": [{"repository": REPO, "number": 161, "head_sha": "7" * 40,
                           "files_changed": [{"filename": REPO_FILE,
                                              "patch": "+      if (error?.code !== UNIQUE_VIOLATION) {"}]}]}]

INTERNAL = "upsertStreamedText then took the 23505 → update path with no error"
OBSERVABLE = ("The response is a judgeRejection (blocked:false). The row exists with user_id = test user A and "
              "soft_refusals=1, and messages/debug_data are filled in.")


def _plan(**sections):
    base = {s: [] for s in ("happy_path", "edge_cases", "integration_tests", "security_negative_tests",
                            "regression_checklist")}
    base.update(sections)
    return SimpleNamespace(**base, shared_preconditions=[])


def _case() -> dict:
    return copy.deepcopy(DATA["case"])


def _line(n: int) -> str:
    return SRC.splitlines()[n - 1]


# --- the fixture is the real code path -------------------------------------------------------

def test_fixture_line_numbers_are_the_real_ones():
    assert _line(11) == "const UNIQUE_VIOLATION = '23505';"
    assert "getStreamedTextByUuid(streamedText.uuid)" in _line(114)
    assert "error?.code !== UNIQUE_VIOLATION" in _line(129)
    assert ".update({" in _line(142) and ".eq('user_id', streamedText.user_id)" in _line(148)
    assert INTERNAL in DATA["case"]["expected"]


# --- 1. Expected keeps only what a tester can observe ----------------------------------------

def test_plan_734_expected_keeps_the_observable_checks_and_moves_the_path_to_the_note():
    case = _case()
    report: dict = {}
    assert ak.check_observable_expected(_plan(edge_cases=[case]), report) == 1
    exp = case["expected"]
    assert exp.startswith(OBSERVABLE)
    for internal in ("23505", "upsertStreamedText", "path"):
        assert internal not in exp
    note = case["mechanism_note"]
    assert note.startswith(ak.NOTE_LABEL) and INTERNAL in note
    # expected_outcome ("The insert race … is handled") named only a mechanism, so it is gone too.
    assert "expected_outcome" not in case and "insert race" in note
    assert [(f["rule"], f["action"]) for f in report["findings"]] == [("AK", "marked")] * 2
    assert "error code 23505" in report["findings"][0]["reason"]
    # Idempotent: a second pass (save time after routing) changes nothing.
    assert ak.check_observable_expected(_plan(edge_cases=[case]), {}) == 0
    assert case["mechanism_note"] == note


def test_an_expected_with_nothing_observable_left_is_marked_not_emptied():
    case = {"title": "t", "steps": ["s"], "expected": "upsertStreamedText then took the 23505 → update path.",
            "expected_verified": True}
    ak.check_observable_expected(_plan(edge_cases=[case]), {})
    assert case["expected"].startswith(ak.UNOBSERVABLE_MARKER) and case["expected_verified"] is False


@pytest.mark.parametrize("expected", [
    "The 'Something went wrong' alert appears (DocumentScreen.kt:90).",
    "HTTP 200. The header equals 'ui-message-stream' (stream-text.ts:242).",
    'TaxableValue 51200 → taxableAmountGlobal "51200.00".',
    "Each rule transitions Normal → Pending → Firing under the forced condition.",
    "A blank estimate name falls back to the calculator product name.",
    "403 'superadmin access required' each time (never 409, 400 or 404).",
    "Both mutations fail with 'Package not found'.",
    "Exactly one streamed_texts row exists for the UUID, with soft_refusals = 1.",
    "Unverified — assumption: writeReview runs on the write and posts to Slack.",
    "The API returns 409 and the handler catches the UNIQUE_VIOLATION.",
])
def test_observable_or_already_labelled_expecteds_are_left_alone(expected):
    kept, moved, _ = ak.split_expected(expected)
    assert moved == [] and kept == expected


@pytest.mark.parametrize("expected,moved_part", [
    ("HTTP 200. The handler takes the v5 branch.", "The handler takes the v5 branch."),
    ("403 'superadmin access required'. The row is unchanged. requireSuperAdmin runs first in Update.",
     "requireSuperAdmin runs first in Update."),
    ("Both calls resolve without throwing: the loser's insert hits 23505, the repository swallows the error.",
     "insert hits 23505"),
    ("Exactly one row exists in streamed_texts because upsertStreamedText took the update path.",
     "upsertStreamedText took the update path."),
])
def test_internal_paths_leave_the_expected(expected, moved_part):
    kept, moved, _ = ak.split_expected(expected)
    assert moved and moved_part in " ".join(moved)
    assert kept and moved_part not in kept


def test_a_rule_q_correction_is_held_to_it_too():
    text = "Re-traced from the code (rule Q): HTTP 200. The handler takes the non-v5 branch."
    kept, moved, _ = ak.split_expected(text)
    assert kept == "Re-traced from the code (rule Q): HTTP 200." and moved == ["The handler takes the non-v5 branch."]


def test_how_to_verify_naming_the_same_code_where_it_shows_counts_as_observable():
    kept, moved, _ = ak.split_expected("The insert fails with 23505 error.",
                                       "In the API logs the line 'insert failed: 23505' appears.")
    assert moved == [] and "23505" in kept


def test_unit_level_cases_assert_the_function_and_are_skipped():
    case = {"title": "t", "steps": ["s"], "expected": "upsertStreamedText takes the update path.",
            "covered_by_unit_test": True}
    assert ak.check_observable_expected(_plan(edge_cases=[case]), {}) == 0


# --- 2. A note naming a path the steps do not reach ------------------------------------------

def test_save_labels_a_race_only_fallback_on_sequential_steps():
    plan = _plan(edge_cases=[_case()])
    report: dict = {}
    pr.recheck_before_save(plan, report)
    case = plan.edge_cases[0]
    assert INTERNAL not in case["expected"]
    assert "Unverified (rule AK): The 23505 fallback normally runs only when another write lands between the " \
           "existence check and the insert" in case["mechanism_note"]
    assert any(f["rule"] == "AK" and "sequential" in f["reason"] for f in report["findings"])


def test_a_case_that_drives_a_concurrent_write_is_not_labelled():
    case = _case()
    case["steps"] = ["Send the first refused message and a second message for the same new UUID at the same time.",
                     "Query the row."]
    plan = _plan(edge_cases=[case])
    pr.recheck_before_save(plan, {})
    assert "Unverified (rule AK)" not in case["mechanism_note"]


class _FakeLLM:
    model = "m"

    def __init__(self, turns):
        self.turns = list(turns)
        self.sent: list = []

    def _budget(self, *a, **k):
        return 1000

    def _effort_kwargs(self, *_a):
        return {}

    async def _post_messages(self, body, **_kw):
        self.sent.append(body)
        return self.turns.pop(0)


class _GH:
    async def fetch_files_at_ref(self, repo, ref, paths, **_):
        return {p: SRC for p in paths if p == REPO_FILE}


ACTUAL = (f"{REPO_FILE}:114 getStreamedTextByUuid finds the row record_chat_refusal created → :116 skips the "
          f"insert → :139-150 owner-scoped update (.eq('user_id') at :148); the response and the row are the same "
          f"as on the update path.")
VERDICT = {"id": "edge_cases:0:p", "verdict": "contradicted", "reached_only_under": "race",
           "traced_path": f"{REPO_FILE}:114 → :116 → :139-150", "actual": ACTUAL,
           "reason": "the 23505 fallback at :128-135 runs only when another insert lands after the check at :114"}


def _turns(verdict, read=True):
    first = {"content": [{"type": "tool_use", "id": "r0", "name": "read_file",
                          "input": {"repo": REPO, "path": REPO_FILE}}]} if read else None
    report = {"content": [{"type": "tool_use", "id": "x", "name": "report_recheck",
                           "input": {"verdicts": [verdict]}}]}
    return [t for t in (first, report) if t]


@pytest.mark.asyncio
async def test_the_trace_critic_replaces_a_note_these_steps_do_not_reach():
    plan = _plan(edge_cases=[_case()])
    report: dict = {}
    llm = _FakeLLM(_turns(VERDICT))
    gaps = await pr.recheck_before_routing(llm, _GH(), plan, DEV, report)
    assert not [g for g in gaps if "error-path" in g]
    case = plan.edge_cases[0]
    # The critic was asked to trace the moved claim from these steps (kind 4).
    first = json.dumps(llm.sent[0])
    assert "edge_cases:0:p" in first and "check kind 4" in first and "23505" in first
    note = case["mechanism_note"]
    assert note.startswith(f"{ak.NOTE_LABEL} {ACTUAL}")
    assert ak.NOT_REACHED_LABEL in note and "runs only under a race" in note
    assert case["mechanism_check"]["verdict"] == "contradicted"
    assert case["mechanism_check"]["reached_only_under"] == "race"
    # The Expected a tester grades never mentions the path.
    assert "23505" not in case["expected"] and case["expected"].startswith(OBSERVABLE)
    # Save time leaves the traced note alone (no second, unverified label).
    pr.recheck_before_save(plan, report)
    assert case["mechanism_note"] == note


@pytest.mark.asyncio
async def test_a_contradiction_resting_on_no_file_read_does_not_replace_the_note():
    plan = _plan(edge_cases=[_case()])
    report: dict = {}
    await pr.recheck_before_routing(_FakeLLM(_turns(VERDICT, read=False)), _GH(), plan, DEV, report)
    case = plan.edge_cases[0]
    assert case["mechanism_check"]["verdict"] == "unverifiable"
    assert INTERNAL in case["mechanism_note"]
    pr.recheck_before_save(plan, report)
    assert "Unverified (rule AK)" in case["mechanism_note"]


@pytest.mark.asyncio
async def test_a_note_the_steps_do_reach_is_kept():
    case = _case()
    case["steps"][0] = "Send two first messages for a new UUID at the same time (two tabs)."
    plan = _plan(edge_cases=[case])
    v = {"id": "edge_cases:0:p", "verdict": "consistent", "reason": "both inserts race",
         "traced_path": f"{REPO_FILE}:114 → :118 insert → :129 UNIQUE_VIOLATION → :139 update"}
    await pr.recheck_before_routing(_FakeLLM(_turns(v)), _GH(), plan, DEV, {})
    assert INTERNAL in case["mechanism_note"] and ak.NOT_REACHED_LABEL not in case["mechanism_note"]
    assert case["mechanism_check"]["verdict"] == "consistent"


def test_code_literals_are_never_cut_at_their_colons_or_commas():
    text = "verifyOtp is called with { token_hash, type: 'recovery' } and setSession is not called."
    _, moved, _ = ak.split_expected(text)
    assert moved == [text]
