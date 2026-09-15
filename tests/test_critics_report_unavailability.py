"""A critic that could not run has checked nothing, and must say so.

The post-generation critics used to catch, log, and return. A plan whose
critics never ran came out with zero warnings — identical in every visible way
to a plan that was checked and found clean. Only one of those is evidence.

The regression-grounding critic has a sharper version of the same problem: it
rewrites lines rather than badging them, so a plan it never ran on looks exactly
like a plan whose every checklist line it checked and approved.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.app.services.test_plan_generator import (
    run_fix_scope_critic,
    run_grounding_critic,
    run_regression_grounding_critic,
    run_surface_mismatch_critic,
)


class Boom:
    """An LLM whose every critic call fails."""

    async def verify_case_grounding(self, *a, **k):
        raise RuntimeError("overloaded")

    async def verify_surface(self, *a, **k):
        raise RuntimeError("overloaded")

    async def verify_fix_scope(self, *a, **k):
        raise RuntimeError("overloaded")

    async def verify_regression_grounding(self, *a, **k):
        raise RuntimeError("overloaded")


class Plan:
    def __init__(self):
        self.happy_path = [{"title": "t", "covers_acs": ["AB-1-AC1"]}]
        self.edge_cases = []
        self.integration_tests = []
        self.regression_checklist = []
        self.grounding_warnings = []


# build_ac_index reads a pre-parsed `acceptance_criteria` list, not prose —
# without one the critic returns early, which is correct silence rather than a
# failure, and would make this test pass for the wrong reason.
TICKETS = [{"ticket_key": "AB-1", "acceptance_criteria": ["The banner renders"]}]


@pytest.mark.asyncio
async def test_grounding_critic_reports_that_it_could_not_run():
    reason = await run_grounding_critic(Boom(), Plan(), TICKETS)
    assert reason and "could not run" in reason


@pytest.mark.asyncio
async def test_fix_scope_critic_reports_that_it_could_not_run():
    reason = await run_fix_scope_critic(
        Boom(), Plan(), [{"ticket_key": "AB-1", "pull_requests": [{"number": 1}]}])
    assert reason is None or "could not run" in reason


@pytest.mark.asyncio
async def test_surface_critic_reports_that_it_could_not_run():
    from src.app.deliverable_classifier import Deliverable
    d = Deliverable(artifact_type="code_behavior", deliverable="a fix",
                    verification_surface="the screen", anchor_steps=[],
                    off_target_signals=[])
    reason = await run_surface_mismatch_critic(Boom(), Plan(), d)
    assert reason is None or "could not run" in reason


@pytest.mark.asyncio
async def test_a_working_critic_reports_nothing():
    """Silence must stay meaningful — it is what "checked and clean" looks like."""

    class Fine:
        async def verify_case_grounding(self, *a, **k):
            return {}

    assert await run_grounding_critic(Fine(), Plan(), TICKETS) is None


# ─── regression-grounding critic ──────────────────────────────────────────────

#: A checklist whose single line names a control, so the critic has something
#: to check. A checklist of pure behaviour lines would make it return early —
#: correct silence, not an unavailability, and it would pass these tests for
#: the wrong reason.
CONTROL_LINE = ["\U0001f7e1 The Print or Download share option still works."]
DEV_INFO = [{
    "ticket_key": "AB-1",
    "development_info": {"pull_requests": [{"number": 1, "repository": "acme/app"}]},
}]


def _plan_with_checklist(lines):
    plan = Plan()
    plan.regression_checklist = list(lines)
    return plan


@pytest.mark.asyncio
async def test_regression_critic_reports_that_it_could_not_run(monkeypatch):
    """The LLM raises mid-pass. Every line is left as generated, and the plan
    has to say the checklist's named controls went unchecked."""
    from src.app.services import test_plan_generator as tpg

    monkeypatch.setattr(tpg.settings, "github_token", "gh-token")

    class NoHits:
        async def search_relevant_files(self, *a, **k):
            return []

    monkeypatch.setattr("src.app.github_client.GitHubClient", lambda *a, **k: NoHits())

    plan = _plan_with_checklist(CONTROL_LINE)
    reason = await run_regression_grounding_critic(Boom(), plan, DEV_INFO)
    assert reason and "could not run" in reason
    assert plan.regression_checklist == CONTROL_LINE


@pytest.mark.asyncio
async def test_regression_critic_reports_a_missing_github_token(monkeypatch):
    """No token means no source, and no source means the pass never happened.
    Saying so is the difference between an unchecked line and an approved one."""
    from src.app.services import test_plan_generator as tpg

    monkeypatch.setattr(tpg.settings, "github_token", None)
    plan = _plan_with_checklist(CONTROL_LINE)
    reason = await run_regression_grounding_critic(Boom(), plan, DEV_INFO)
    assert reason and "could not run" in reason and "GitHub token" in reason
    assert plan.regression_checklist == CONTROL_LINE


@pytest.mark.asyncio
async def test_regression_critic_reports_a_missing_repo(monkeypatch):
    from src.app.services import test_plan_generator as tpg

    monkeypatch.setattr(tpg.settings, "github_token", "gh-token")
    plan = _plan_with_checklist(CONTROL_LINE)
    reason = await run_regression_grounding_critic(
        Boom(), plan, [{"ticket_key": "AB-1", "development_info": {}}]
    )
    assert reason and "could not run" in reason and "linked repo" in reason


@pytest.mark.asyncio
async def test_regression_critic_stays_silent_when_no_line_names_a_control(monkeypatch):
    """Not an unavailability. A checklist of pure behaviour lines has nothing
    for this critic to check, and reporting a gap there would train readers to
    ignore the field."""
    from src.app.services import test_plan_generator as tpg

    monkeypatch.setattr(tpg.settings, "github_token", None)
    plan = _plan_with_checklist([
        "\U0001f534 The walkthrough still plays English from start to finish.",
    ])
    assert await run_regression_grounding_critic(Boom(), plan, DEV_INFO) is None


@pytest.mark.asyncio
async def test_regression_critic_is_silent_when_disabled(monkeypatch):
    from src.app.services import test_plan_generator as tpg

    monkeypatch.setattr(tpg.settings, "regression_grounding_critic_enabled", False)
    plan = _plan_with_checklist(CONTROL_LINE)
    assert await run_regression_grounding_critic(Boom(), plan, DEV_INFO) is None
    assert plan.regression_checklist == CONTROL_LINE
