"""Rule AL: tool-call markup never reaches a stored plan field.

SK-3014 plan 734 v1 stored ``how_to_see_it`` as the string
``'\\n<parameter name="summary">Nothing changes on screen. …'`` — the model
wrote the object as a nested tool-call parameter, and the Pass-to-UAT form,
the Loom description and the Jira comment all showed the envelope to the PM.
"""
import asyncio
import re

import pytest

from src.app.llm_client import SUBMIT_TEST_PLAN_TOOL, _plan_tool_input, _scrub_test_plan_data
from src.app.services.plan_service import _body_schema, _recheck_tool_markup
from src.app.tool_markup import has_tool_markup, normalize_plan_fields, recheck_tool_markup

SCHEMA = SUBMIT_TEST_PLAN_TOOL["input_schema"]
MARKUP = re.compile(r"</?\s*(?:\w+:)?(?:parameter|invoke|function_calls)\b")

SK3014 = ('\n<parameter name="summary">Nothing changes on screen. When the server refuses chat '
          'messages, it now keeps the "this chat is locked" flag and the soft-warning count in '
          'separate database fields.')


def _case(title="t", **kw):
    return {"title": title, "priority": "high", "steps": ["open it"], "expected": "it works", **kw}


def _no_markup_anywhere(value) -> bool:
    if isinstance(value, str):
        return not MARKUP.search(value) and value == value.strip()
    if isinstance(value, dict):
        return all(_no_markup_anywhere(v) for v in value.values())
    if isinstance(value, list):
        return all(_no_markup_anywhere(v) for v in value)
    return True


class FakeLLM:
    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    async def regenerate_plan_field(self, field, schema, context):
        self.asked.append((field, schema, context))
        return self.answers.pop(0) if self.answers else None


# --- the SK-3014 shape, end to end --------------------------------------------


def test_nested_parameter_envelope_in_how_to_see_it_is_saved_as_clean_prose():
    """The regression: how_to_see_it arrives as a string holding a nested
    <parameter name="summary"> envelope. Parsed from the tool call and run
    through the save-time re-check, the stored field is clean prose and the
    lost ``reason`` is re-asked for, not saved missing or as markup."""
    content = [{"type": "tool_use", "name": "submit_test_plan", "input": {
        "happy_path": [_case()], "edge_cases": [], "regression_checklist": ["  check login  "],
        "uat_complexity": "high", "how_to_see_it": SK3014,
    }}]
    plan = _scrub_test_plan_data(_plan_tool_input(content))
    hts = plan["how_to_see_it"]
    assert isinstance(hts, dict)
    assert hts["summary"].startswith("Nothing changes on screen.")
    assert not has_tool_markup(hts["summary"])
    assert plan["regression_checklist"] == ["check login"]

    body = {"ticket_key": "SK-3014", **plan, "source_provenance": {}}
    llm = FakeLLM(["Backend-only change that needs direct API calls and a database read."])
    asyncio.run(_recheck_tool_markup(llm, body, body["source_provenance"]))

    assert body["how_to_see_it"] == {
        "summary": ('Nothing changes on screen. When the server refuses chat messages, it now keeps '
                    'the "this chat is locked" flag and the soft-warning count in separate database fields.'),
        "reason": "Backend-only change that needs direct API calls and a database read.",
    }
    assert [a[0] for a in llm.asked] == ["how_to_see_it.reason"]
    assert _no_markup_anywhere(body)
    report = body["source_provenance"]["plan_recheck"]
    assert report["tool_markup"]["reasked"] == ["how_to_see_it.reason"]
    assert report["tool_markup"]["unresolved"] == []
    assert all(f["rule"] == "AL" and f["action"] == "fixed" for f in report["findings"])
    assert report["fixed"] == len(report["findings"])


# --- the general rule ---------------------------------------------------------


NS = "antml:"  # a namespaced envelope, built here so this file never holds a literal tag
OPEN_EXP = '<parameter name="expected">'
CLOSE = "</parameter>"
NS_CLOSE = "</" + NS + "parameter>"
NS_INVOKE = "<" + NS + 'invoke name="submit_test_plan">'


@pytest.mark.parametrize("dirty", [
    OPEN_EXP + "The banner shows" + CLOSE,
    "\n" + OPEN_EXP + "The banner shows" + NS_CLOSE + "\n",
    NS_INVOKE + "The banner shows",
    "  The banner shows" + CLOSE + "  ",
    "<function_calls>The banner shows</function_calls>",
])
def test_envelope_tags_are_stripped_from_case_fields(dirty):
    plan = {"edge_cases": [_case(expected=dirty)]}
    result = normalize_plan_fields(plan, SCHEMA)
    assert plan["edge_cases"][0]["expected"] == "The banner shows"
    assert result["fixed"] == [["edge_cases", 0, "expected"]]
    assert result["failures"] == []


def test_envelope_that_runs_two_fields_together_is_split_back():
    """summary text, then the reason parameter, all in one string."""
    plan = {"how_to_see_it": {"summary": "Open Billing and click Upgrade." + CLOSE
                                         + '\n<parameter name="reason">Needs a Pro account.'}}
    normalize_plan_fields(plan, SCHEMA)
    assert plan["how_to_see_it"] == {"summary": "Open Billing and click Upgrade.",
                                     "reason": "Needs a Pro account."}


def test_string_where_schema_wants_object_is_rebuilt_from_named_parameters():
    plan = {"how_to_see_it": '<parameter name="summary">See it on Billing.' + CLOSE
                             + '<parameter name="reason">One screen, no setup.' + CLOSE}
    result = normalize_plan_fields(plan, SCHEMA)
    assert plan["how_to_see_it"] == {"summary": "See it on Billing.", "reason": "One screen, no setup."}
    assert result["failures"] == []


def test_plain_strings_only_lose_outer_whitespace():
    plan = {"happy_path": [_case(title="  Upgrade works \n", expected="Shows <b>Pro</b> badge")],
            "regression_checklist": ["Log in still works"]}
    result = normalize_plan_fields(plan, SCHEMA)
    assert plan["happy_path"][0]["title"] == "Upgrade works"
    # Ordinary angle brackets in prose are not tool-call markup.
    assert plan["happy_path"][0]["expected"] == "Shows <b>Pro</b> badge"
    assert result["fixed"] == [] and result["trimmed"] == 1


def test_field_that_was_only_markup_is_reasked_and_the_answer_validated():
    body = {"edge_cases": [_case(title="Locked chat", expected=OPEN_EXP + CLOSE)]}
    llm = FakeLLM([OPEN_EXP + "still markup"])
    report = {}
    asyncio.run(recheck_tool_markup(llm, body, report, schema=SCHEMA))
    # One re-ask per failing field, and its answer is validated like the
    # first: it still carried markup, so it is refused, not saved.
    assert len(llm.asked) == 1
    assert body["edge_cases"][0]["expected"] == ""
    assert report["tool_markup"]["unresolved"] == ["edge_cases[0].expected"]
    marked = [f for f in report["findings"] if f["action"] == "marked"]
    assert marked and marked[0]["title"] == "Locked chat" and marked[0]["rule"] == "AL"


def test_reask_that_cannot_run_marks_the_field_and_never_saves_markup():
    body = {"how_to_see_it": SK3014, "source_provenance": {"untouched": "  x  "}}
    report = {}
    asyncio.run(recheck_tool_markup(object(), body, report, schema=SCHEMA))
    assert body["how_to_see_it"]["summary"].startswith("Nothing changes on screen.")
    assert "reason" not in body["how_to_see_it"]
    assert report["tool_markup"]["unresolved"] == ["how_to_see_it.reason"]
    assert body["source_provenance"] == {"untouched": "  x  "}  # not model text; left alone
    assert _no_markup_anywhere({k: v for k, v in body.items() if k != "source_provenance"})


def test_clean_plan_is_untouched_and_records_nothing():
    body = {"happy_path": [_case()], "how_to_see_it": {"summary": "s", "reason": "r"}}
    report = {}
    llm = FakeLLM([])
    asyncio.run(recheck_tool_markup(llm, body, report, schema=SCHEMA))
    assert "tool_markup" not in report and report.get("findings") == [] and llm.asked == []


def test_reask_answer_is_accepted_when_clean():
    body = {"edge_cases": [_case(title="Locked chat", expected=OPEN_EXP + CLOSE)]}
    llm = FakeLLM(["  The API answers 423 and the chat stays locked. "])
    report = {}
    asyncio.run(recheck_tool_markup(llm, body, report, schema=SCHEMA))
    assert body["edge_cases"][0]["expected"] == "The API answers 423 and the chat stays locked."
    assert report["tool_markup"]["reasked"] == ["edge_cases[0].expected"]


def test_cases_moved_to_needs_spec_are_checked_with_the_case_schema():
    body = {"needs_spec_cases": [_case(expected="\n" + OPEN_EXP + "Shows the lock")]}
    report = {}
    asyncio.run(recheck_tool_markup(FakeLLM([]), body, report,
                                    schema=_body_schema(SUBMIT_TEST_PLAN_TOOL)))
    assert body["needs_spec_cases"][0]["expected"] == "Shows the lock"


def test_argument_the_envelope_pushed_to_the_top_level_is_folded_back():
    """What the model actually sends (SK-2826 eval, 2026-11-18): how_to_see_it
    closed after summary, and ``reason`` as a top-level argument of its own.
    It is taken from the arguments, not re-asked and not dropped."""
    content = [{"type": "tool_use", "name": "submit_test_plan", "input": {
        "happy_path": [_case()], "uat_complexity": "high", "how_to_see_it": SK3014,
        "reason": "Backend-only: needs direct API calls and a database read.",
    }}]
    plan = _scrub_test_plan_data(_plan_tool_input(content))
    assert "reason" not in plan
    assert plan["how_to_see_it"]["reason"] == "Backend-only: needs direct API calls and a database read."
    assert plan["how_to_see_it"]["summary"].startswith("Nothing changes on screen.")

    body = {"ticket_key": "SK-3014", **plan, "source_provenance": {}}
    llm = FakeLLM([])
    asyncio.run(_recheck_tool_markup(llm, body, body["source_provenance"]))
    assert llm.asked == []  # nothing left to re-ask for


def test_unrelated_top_level_key_is_not_folded_into_a_clean_object():
    plan = {"how_to_see_it": {"summary": "s", "reason": "r"}, "reason": "something else"}
    normalize_plan_fields(plan, SCHEMA)
    assert plan == {"how_to_see_it": {"summary": "s", "reason": "r"}, "reason": "something else"}
