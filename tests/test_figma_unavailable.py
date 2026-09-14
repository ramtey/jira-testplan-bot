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


class TestOtherSourcesReportGapsToo:
    """Confluence and Slack, same rule: linked-and-unreadable != absent."""

    def _prompt(self, gaps):
        from src.app.llm_client import LLMClient
        return LLMClient._build_prompt(
            None, ticket_key="AB-1", summary="s", description="d",
            testing_context={}, development_info=None, context_gaps=gaps,
        )

    def test_gaps_are_named_in_the_prompt(self):
        p = self._prompt(["2 of 3 linked Slack message(s) could not be read"])
        assert "CONTEXT THIS TICKET LINKS THAT COULD NOT BE READ" in p
        assert "2 of 3 linked Slack message(s)" in p

    def test_the_model_is_told_not_to_guess_their_contents(self):
        p = self._prompt(["1 linked Confluence spec page(s) could not be read"])
        assert "Do NOT guess what these said" in p

    def test_no_gaps_means_no_block(self):
        assert "CONTEXT THIS TICKET LINKS THAT COULD NOT BE READ" not in self._prompt([])


class TestConfluenceGaps:
    @pytest.mark.asyncio
    async def test_a_ticket_with_no_spec_link_reports_nothing(self):
        from src.app.confluence_client import ConfluenceClient
        pages, gaps = await ConfluenceClient().fetch_pages_from_text("no links here")
        assert (pages, gaps) == ([], [])

    @pytest.mark.asyncio
    async def test_a_linked_spec_with_no_credentials_is_a_gap(self, monkeypatch):
        # Previously this returned [] — identical to "this ticket has no spec".
        from src.app import confluence_client as cc
        monkeypatch.setattr(cc.settings, "jira_url", "")
        text = "spec: https://acme.atlassian.net/wiki/spaces/ENG/pages/12345/Title"
        pages, gaps = await cc.ConfluenceClient().fetch_pages_from_text(text)
        assert pages == []
        assert gaps and "no Jira credentials" in gaps[0]
