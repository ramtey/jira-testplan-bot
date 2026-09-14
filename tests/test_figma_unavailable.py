"""A design that couldn't be read must not look like a ticket with no design.

This is the shape that cost every plan its design context for an unknown
stretch: the Figma token expired, the fetch returned None, and None is also
what a ticket with no Figma link returns. The plan simply contained no visual
checks, and read as though none were needed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.app.models import GenerateTestPlanRequest
from src.app.services.plan_service import prompt_payload


class TestTheFlagSurvivesSerialisation:
    def test_prompt_payload_carries_it(self):
        assert prompt_payload({
            "key": "AB-1", "summary": "s", "issue_type": "Bug",
            "figma_unavailable": True,
        })["figma_unavailable"] is True

    def test_absent_means_false_not_missing(self):
        # A ticket with no designs is not a ticket whose designs failed.
        assert prompt_payload({
            "key": "AB-1", "summary": "s", "issue_type": "Bug",
        })["figma_unavailable"] is False

    def test_the_request_model_accepts_it(self):
        r = GenerateTestPlanRequest(
            ticket_key="AB-1", summary="s", issue_type="Bug", figma_unavailable=True)
        assert r.figma_unavailable is True


class TestThePromptSaysSo:
    def _prompt(self, dev_info):
        # _build_prompt lives on the abstract base, so it is the one prompt
        # both providers share — testing it covers Claude and Ollama alike.
        from src.app.llm_client import LLMClient
        return LLMClient._build_prompt(
            None, ticket_key="AB-1", summary="Restyle the banner",
            description="Match the new design.", testing_context={},
            development_info=dev_info,
        )

    def test_an_unreadable_design_is_called_out(self):
        p = self._prompt({"figma_unavailable": True})
        assert "DESIGN SPECIFICATIONS — UNAVAILABLE" in p
        assert "You have NOT seen the design" in p

    def test_and_the_model_is_told_not_to_invent_what_it_says(self):
        p = self._prompt({"figma_unavailable": True})
        assert "Do NOT write any case asserting that something matches the" in p

    def test_a_ticket_with_no_design_gets_no_such_warning(self):
        # Silence is right here — there was nothing to read.
        assert "DESIGN SPECIFICATIONS — UNAVAILABLE" not in self._prompt({})
