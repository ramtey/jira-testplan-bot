"""An all-empty plan is a failed generation, never a 0-case success.

Replaying SK-1998 on 2026-10-06, one of four runs came back with nothing in
any section and was saved and reported as "ok (0 cases)". Graded, it would
have read as the plan missing every problem.
"""
import pytest

from src.app.llm_client import LLMError, _require_cases


def test_empty_tool_input_raises():
    with pytest.raises(LLMError) as e:
        _require_cases({}, {"stop_reason": "tool_use"})
    assert "no cases in any section" in str(e.value)


def test_sections_present_but_empty_raises_and_names_what_came_back():
    with pytest.raises(LLMError) as e:
        _require_cases({"happy_path": [], "edge_cases": [], "risks_and_gaps": ["x"]},
                       {"stop_reason": "end_turn"})
    assert "risks_and_gaps" in str(e.value)


@pytest.mark.parametrize("section", ["happy_path", "edge_cases", "integration_tests", "regression_checklist"])
def test_any_one_section_with_cases_is_a_plan(section):
    _require_cases({section: [{"title": "t"}]}, {})
