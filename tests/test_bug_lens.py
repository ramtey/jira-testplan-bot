"""
Tests for Jira Bug Lens — bug analysis endpoints, models, and prompt builder.

Covers:
- BugAnalysis model structure
- POST /bug-lens/analyze (single ticket)
- POST /bug-lens/analyze/multi (multiple tickets)
- LLM error handling (503)
- _build_bug_analysis_prompt content for single, multi, and PR-enriched tickets
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.app.main import app
from src.app.models import BugAnalysis
from src.app.llm_client import OllamaClient

client = TestClient(app)


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_bug_analysis(is_fixed=True):
    return BugAnalysis(
        bug_summary="Button click triggers duplicate API calls due to missing debounce.",
        root_cause="The onClick handler in SubmitButton.tsx was not guarded, allowing rapid re-clicks.",
        is_fixed=is_fixed,
        fix_explanation="Added a `disabled` flag set on first click and cleared after the API response." if is_fixed else None,
        regression_tests=[
            "Click 'Submit' rapidly — only one API call should be made.",
            "Click 'Submit', wait for response, click again — second call should proceed normally.",
        ],
        similar_patterns=[
            "Other form submit buttons without debounce or disabled-on-submit guards.",
            "Async handlers that mutate shared state without loading flags.",
        ],
        fix_complexity=None if is_fixed else "trivial",
        fix_effort_estimate=None if is_fixed else "1–2 hours",
        fix_complexity_reasoning=None if is_fixed else "Single-line guard in one component.",
    )


def mock_llm(analysis: BugAnalysis):
    """Return a mock LLM client whose bug analysis methods return the given BugAnalysis."""
    llm = MagicMock()
    llm.generate_bug_analysis = AsyncMock(return_value=analysis)
    llm.generate_multi_bug_analysis = AsyncMock(return_value=analysis)
    return llm


# ── Model tests ────────────────────────────────────────────────────────────────

def test_bug_analysis_model_fields():
    """BugAnalysis dataclass has all expected fields with correct types."""
    analysis = make_bug_analysis()
    assert isinstance(analysis.bug_summary, str)
    assert isinstance(analysis.root_cause, (str, type(None)))
    assert isinstance(analysis.is_fixed, bool)
    assert isinstance(analysis.fix_explanation, (str, type(None)))
    assert isinstance(analysis.regression_tests, list)
    assert isinstance(analysis.similar_patterns, list)


def test_bug_analysis_unfixed_has_no_fix_explanation():
    """When is_fixed=False, fix_explanation must be None."""
    analysis = make_bug_analysis(is_fixed=False)
    assert analysis.is_fixed is False
    assert analysis.fix_explanation is None


def test_bug_analysis_fixed_has_explanation():
    """When is_fixed=True, fix_explanation is populated."""
    analysis = make_bug_analysis(is_fixed=True)
    assert analysis.is_fixed is True
    assert analysis.fix_explanation is not None


# ── API: single ticket ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_analyze_bug_single_ticket_success():
    """POST /bug-lens/analyze returns analysis with correct ticket_key."""
    analysis = make_bug_analysis()

    with patch("src.app.bug_lens_routes.get_llm_client", return_value=mock_llm(analysis)):
        response = client.post("/bug-lens/analyze", json={
            "ticket_key": "BUG-42",
            "summary": "Duplicate API calls on submit",
            "issue_type": "Bug",
        })

    assert response.status_code == 200
    data = response.json()
    assert data["ticket_key"] == "BUG-42"
    assert data["bug_summary"] == analysis.bug_summary
    assert data["is_fixed"] is True
    assert data["fix_explanation"] == analysis.fix_explanation
    assert len(data["regression_tests"]) == 2
    assert len(data["similar_patterns"]) == 2


@pytest.mark.asyncio
async def test_analyze_bug_single_ticket_unfixed():
    """POST /bug-lens/analyze returns is_fixed=False and null fix_explanation."""
    analysis = make_bug_analysis(is_fixed=False)

    with patch("src.app.bug_lens_routes.get_llm_client", return_value=mock_llm(analysis)):
        response = client.post("/bug-lens/analyze", json={
            "ticket_key": "BUG-99",
            "summary": "App crashes on startup",
            "issue_type": "Bug",
        })

    assert response.status_code == 200
    data = response.json()
    assert data["is_fixed"] is False
    assert data["fix_explanation"] is None


@pytest.mark.asyncio
async def test_analyze_bug_passes_development_info_to_llm():
    """POST /bug-lens/analyze forwards development_info to the LLM client."""
    analysis = make_bug_analysis()
    llm = mock_llm(analysis)

    dev_info = {
        "pull_requests": [{"title": "Fix duplicate calls", "status": "MERGED"}],
        "commits": [],
        "branches": [],
    }

    with patch("src.app.bug_lens_routes.get_llm_client", return_value=llm):
        client.post("/bug-lens/analyze", json={
            "ticket_key": "BUG-42",
            "summary": "Duplicate API calls",
            "issue_type": "Bug",
            "development_info": dev_info,
        })

    llm.generate_bug_analysis.assert_called_once()
    call_kwargs = llm.generate_bug_analysis.call_args.kwargs
    assert call_kwargs["development_info"] == dev_info


@pytest.mark.asyncio
async def test_analyze_bug_llm_error_returns_503():
    """POST /bug-lens/analyze returns 503 when the LLM raises LLMError."""
    from src.app.llm_client import LLMError

    llm = MagicMock()
    llm.generate_bug_analysis = AsyncMock(side_effect=LLMError("LLM unavailable"))

    with patch("src.app.bug_lens_routes.get_llm_client", return_value=llm):
        response = client.post("/bug-lens/analyze", json={
            "ticket_key": "BUG-1",
            "summary": "Something broke",
            "issue_type": "Bug",
        })

    assert response.status_code == 503
    assert "LLM unavailable" in response.json()["detail"]


# ── API: multi-ticket ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_analyze_bugs_multi_ticket_success():
    """POST /bug-lens/analyze/multi returns analysis with all ticket_keys."""
    analysis = make_bug_analysis()

    with patch("src.app.bug_lens_routes.get_llm_client", return_value=mock_llm(analysis)):
        response = client.post("/bug-lens/analyze/multi", json={
            "tickets": [
                {"ticket_key": "BUG-10", "summary": "Crash on login", "issue_type": "Bug"},
                {"ticket_key": "BUG-11", "summary": "Crash on logout", "issue_type": "Bug"},
            ]
        })

    assert response.status_code == 200
    data = response.json()
    assert data["ticket_keys"] == ["BUG-10", "BUG-11"]
    assert data["bug_summary"] == analysis.bug_summary
    assert isinstance(data["regression_tests"], list)


@pytest.mark.asyncio
async def test_analyze_bugs_multi_ticket_passes_tickets_to_llm():
    """POST /bug-lens/analyze/multi passes all tickets to generate_multi_bug_analysis."""
    analysis = make_bug_analysis()
    llm = mock_llm(analysis)

    with patch("src.app.bug_lens_routes.get_llm_client", return_value=llm):
        client.post("/bug-lens/analyze/multi", json={
            "tickets": [
                {"ticket_key": "BUG-10", "summary": "A", "issue_type": "Bug"},
                {"ticket_key": "BUG-11", "summary": "B", "issue_type": "Bug"},
            ]
        })

    llm.generate_multi_bug_analysis.assert_called_once()
    tickets_arg = llm.generate_multi_bug_analysis.call_args.kwargs["tickets"]
    assert len(tickets_arg) == 2
    assert tickets_arg[0]["ticket_key"] == "BUG-10"
    assert tickets_arg[1]["ticket_key"] == "BUG-11"


@pytest.mark.asyncio
async def test_analyze_bugs_multi_llm_error_returns_503():
    """POST /bug-lens/analyze/multi returns 503 when the LLM raises LLMError."""
    from src.app.llm_client import LLMError

    llm = MagicMock()
    llm.generate_multi_bug_analysis = AsyncMock(side_effect=LLMError("Rate limited"))

    with patch("src.app.bug_lens_routes.get_llm_client", return_value=llm):
        response = client.post("/bug-lens/analyze/multi", json={
            "tickets": [
                {"ticket_key": "BUG-10", "summary": "A", "issue_type": "Bug"},
            ]
        })

    assert response.status_code == 503


# ── Prompt builder ─────────────────────────────────────────────────────────────

def test_prompt_contains_ticket_key_and_summary():
    """Prompt includes the ticket key and summary."""
    llm = OllamaClient()
    prompt = llm._build_bug_analysis_prompt([{
        "ticket_key": "BUG-42",
        "summary": "Button triggers duplicate calls",
        "description": None,
        "development_info": None,
        "comments": None,
        "linked_info": None,
    }])
    assert "BUG-42" in prompt
    assert "Button triggers duplicate calls" in prompt


def test_prompt_includes_description():
    """Prompt includes the ticket description when provided."""
    llm = OllamaClient()
    prompt = llm._build_bug_analysis_prompt([{
        "ticket_key": "BUG-1",
        "summary": "Crash",
        "description": "App crashes when user taps the back button on the payment screen.",
        "development_info": None,
        "comments": None,
        "linked_info": None,
    }])
    assert "App crashes when user taps the back button" in prompt


def test_prompt_handles_missing_description():
    """Prompt renders a fallback when description is None."""
    llm = OllamaClient()
    prompt = llm._build_bug_analysis_prompt([{
        "ticket_key": "BUG-1",
        "summary": "Crash",
        "description": None,
        "development_info": None,
        "comments": None,
        "linked_info": None,
    }])
    assert "No description provided" in prompt


def test_prompt_includes_pr_info_and_diff():
    """Prompt includes PR title, status, and code diff when development_info is present."""
    llm = OllamaClient()
    dev_info = {
        "pull_requests": [{
            "title": "Fix duplicate submit calls",
            "status": "MERGED",
            "source_branch": "fix/duplicate-submit",
            "github_description": "Adds disabled flag to prevent re-clicks.",
            "files_changed": [{
                "filename": "src/components/SubmitButton.tsx",
                "status": "modified",
                "additions": 5,
                "deletions": 1,
                "changes": 6,
                "patch": "-  onClick={handleSubmit}\n+  onClick={handleSubmit} disabled={loading}",
            }],
            "total_additions": 5,
            "total_deletions": 1,
            "comments": [],
        }],
        "commits": [],
        "branches": [],
    }
    prompt = llm._build_bug_analysis_prompt([{
        "ticket_key": "BUG-42",
        "summary": "Duplicate calls",
        "description": None,
        "development_info": dev_info,
        "comments": None,
        "linked_info": None,
    }])
    assert "Fix duplicate submit calls" in prompt
    assert "MERGED" in prompt
    assert "SubmitButton.tsx" in prompt
    assert "disabled={loading}" in prompt


def test_prompt_marks_merged_pr_as_fixed():
    """Prompt labels a merged PR as fixed."""
    llm = OllamaClient()
    dev_info = {
        "pull_requests": [{
            "title": "Fix crash",
            "status": "MERGED",
            "files_changed": [],
            "total_additions": 0,
            "total_deletions": 0,
            "comments": [],
        }],
        "commits": [],
        "branches": [],
    }
    prompt = llm._build_bug_analysis_prompt([{
        "ticket_key": "BUG-1",
        "summary": "Crash",
        "description": None,
        "development_info": dev_info,
        "comments": None,
        "linked_info": None,
    }])
    assert "merged" in prompt.lower()


def test_prompt_multi_ticket_includes_all_keys():
    """Multi-ticket prompt includes each ticket key and summary."""
    llm = OllamaClient()
    prompt = llm._build_bug_analysis_prompt([
        {"ticket_key": "BUG-10", "summary": "Crash on login", "description": None,
         "development_info": None, "comments": None, "linked_info": None},
        {"ticket_key": "BUG-11", "summary": "Crash on logout", "description": None,
         "development_info": None, "comments": None, "linked_info": None},
    ])
    assert "BUG-10" in prompt
    assert "BUG-11" in prompt
    assert "Crash on login" in prompt
    assert "Crash on logout" in prompt


def test_prompt_includes_jira_comments():
    """Prompt includes Jira comment bodies."""
    llm = OllamaClient()
    prompt = llm._build_bug_analysis_prompt([{
        "ticket_key": "BUG-5",
        "summary": "Crash",
        "description": None,
        "development_info": None,
        "comments": [{"author": "alice", "body": "Reproduced on iOS 17 only.", "created": "2024-01-01"}],
        "linked_info": None,
    }])
    assert "Reproduced on iOS 17 only." in prompt
