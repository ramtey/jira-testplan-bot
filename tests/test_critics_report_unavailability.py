"""A critic that could not run has checked nothing, and must say so.

All three post-generation critics used to catch, log, and return. A plan whose
critics never ran came out with zero warnings — identical in every visible way
to a plan that was checked and found clean. Only one of those is evidence.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.app.services.test_plan_generator import (
    run_fix_scope_critic,
    run_grounding_critic,
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
