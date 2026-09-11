"""A failed lookup must never be reported as an answer.

Two bugs, one shape — the codebase states something it doesn't know:

* Jira's dev-status endpoint returning None on a timeout, which
  ``require_source_grounding`` then published as "No implementation was
  found for this ticket … link a pull request" for a ticket that may well
  have a merged PR.
* The watcher deciding "merged" from Jira's status string alone, which
  lags GitHub — so a ticket whose PR had landed was skipped every sweep.

``code_grounding_critic`` already learned this lesson ("treating a throttle
as evidence is what made this critic quietly report false negatives"); these
are the two places that hadn't.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.app.jira_client import JiraClient
from src.app.models import GenerateTestPlanRequest
from src.app.services import plan_service
from src.app.services.queue_watcher import _has_merged_pr


def _transport(handler):
    """Stub httpx.AsyncClient with `handler(url) -> Response | raise`."""

    class _Stub:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return handler(url)

    return _Stub


class TestDevStatusAbsenceVsSilence:
    """`_get_development_info` must say which of the two it is."""

    @pytest.mark.asyncio
    async def test_timeout_reports_unavailable(self):
        def boom(url):
            raise httpx.ReadTimeout("dev-status slow")

        with patch("src.app.jira_client.httpx.AsyncClient", _transport(boom)):
            info, unavailable = await JiraClient()._get_development_info("SK-1", "10001")
        assert info is None
        assert unavailable is True

    @pytest.mark.asyncio
    async def test_rate_limit_reports_unavailable(self):
        def throttled(url):
            return httpx.Response(429, request=httpx.Request("GET", url))

        with patch("src.app.jira_client.httpx.AsyncClient", _transport(throttled)):
            info, unavailable = await JiraClient()._get_development_info("SK-1", "10001")
        assert info is None
        assert unavailable is True

    @pytest.mark.asyncio
    async def test_404_is_genuine_absence(self):
        # Jira has no dev-status record for the issue. That IS an answer.
        def missing(url):
            return httpx.Response(404, request=httpx.Request("GET", url))

        with patch("src.app.jira_client.httpx.AsyncClient", _transport(missing)):
            info, unavailable = await JiraClient()._get_development_info("SK-1", "10001")
        assert info is None
        assert unavailable is False

    @pytest.mark.asyncio
    async def test_empty_summary_is_genuine_absence(self):
        def empty(url):
            return httpx.Response(
                200, request=httpx.Request("GET", url), json={"summary": {}}
            )

        with patch("src.app.jira_client.httpx.AsyncClient", _transport(empty)):
            info, unavailable = await JiraClient()._get_development_info("SK-1", "10001")
        assert info is None
        assert unavailable is False

    @pytest.mark.asyncio
    async def test_detail_call_failing_reports_unavailable(self):
        # The summary said GitHub data exists; the detail call then 500s. We
        # end up with zero PRs and no idea whether that's true.
        def partial(url):
            request = httpx.Request("GET", url)
            if "summary" in url:
                return httpx.Response(
                    200,
                    request=request,
                    json={"summary": {"pullrequest": {"byInstanceType": {"GitHub": {}}}}},
                )
            return httpx.Response(500, request=request)

        with patch("src.app.jira_client.httpx.AsyncClient", _transport(partial)):
            info, unavailable = await JiraClient()._get_development_info("SK-1", "10001")
        assert info is None
        assert unavailable is True


class TestNoSourceRefusal:
    """The refusal is a factual claim, so it needs a fact behind it."""

    @staticmethod
    def _request(**kw):
        return GenerateTestPlanRequest(
            ticket_key="SK-9999",
            summary="Save a listing",
            issue_type="Story",
            **kw,
        )

    @pytest.mark.asyncio
    async def test_unknown_source_fails_instead_of_claiming_none(self, monkeypatch):
        monkeypatch.setattr(
            plan_service.settings, "require_source_grounding", True, raising=False
        )
        with pytest.raises(plan_service.SourceLookupUnavailableError) as excinfo:
            await plan_service.generate_single(
                self._request(dev_status_unavailable=True)
            )
        message = str(excinfo.value)
        assert "SK-9999" in message
        assert "temporary" in message
        assert "No implementation was found" not in message

    @pytest.mark.asyncio
    async def test_a_real_absence_still_refuses_with_the_no_source_result(
        self, monkeypatch
    ):
        # The check this guard protects must keep working: a ticket the
        # lookup did reach, with no PR, still gets the refusal.
        monkeypatch.setattr(
            plan_service.settings, "require_source_grounding", True, raising=False
        )
        result = await plan_service.generate_single(self._request())
        assert result["no_source"] is True
        assert "No implementation was found" in result["no_source_message"]

    @pytest.mark.asyncio
    async def test_multi_refuses_when_any_ticket_is_unknown(self, monkeypatch):
        from src.app.models import TicketInput

        monkeypatch.setattr(
            plan_service.settings, "require_source_grounding", True, raising=False
        )
        tickets = [
            TicketInput(ticket_key="SK-1", summary="a", issue_type="Story"),
            TicketInput(
                ticket_key="SK-2",
                summary="b",
                issue_type="Story",
                dev_status_unavailable=True,
            ),
        ]
        with pytest.raises(plan_service.SourceLookupUnavailableError) as excinfo:
            await plan_service.generate_multi(tickets)
        assert "SK-2" in str(excinfo.value)


class TestWatcherMergedDetection:
    """One definition of "merged", and it's `classify_pr_state`."""

    @staticmethod
    def _payload(pr):
        return {"development_info": {"pull_requests": [pr]}}

    def test_merged_at_wins_over_a_lagging_jira_mirror(self):
        # GitHub says it landed; Jira's dev-status mirror still says OPEN.
        # Reading the status string alone skipped this ticket forever.
        pr = {"status": "OPEN", "merged_at": "2026-09-10T12:00:00Z"}
        assert _has_merged_pr(self._payload(pr)) is True

    def test_plain_merged_status(self):
        assert _has_merged_pr(self._payload({"status": "MERGED"})) is True

    def test_open_pr_is_not_merged(self):
        assert _has_merged_pr(self._payload({"status": "OPEN"})) is False

    def test_declined_pr_is_not_merged(self):
        assert _has_merged_pr(self._payload({"status": "DECLINED"})) is False

    def test_blank_merged_at_does_not_count(self):
        assert _has_merged_pr(self._payload({"status": "OPEN", "merged_at": "  "})) is False

    def test_no_development_info(self):
        assert _has_merged_pr({}) is False
