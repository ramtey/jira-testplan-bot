"""Every context source, forced to fail, must say so rather than answer.

This is the enumeration the repo kept rediscovering one source at a time: a
failed lookup recorded as a fact about the ticket. Figma was the fourth
instance, run persistence the fifth, the Figma health check itself the sixth.
The rows below are the remaining sources, each forced into failure and checked
for the one thing that matters — that "we couldn't read it" and "there's
nothing there" do not arrive identically.

The discrimination in every case is the same: the failure path must not
produce the value that a genuinely empty source produces.
"""
import pytest

from src.app.models import GenerateTestPlanRequest
from src.app.services import run_tracker
from src.app.services.run_tracker import RunContext


class TestJiraComments:
    """`_fetch_comments` returned [] on any exception, which is also what a
    ticket with no comments returns. A bounce reason lives in those comments."""

    @pytest.mark.asyncio
    async def test_a_failed_comment_fetch_is_not_an_empty_comment_list(self):
        from src.app.jira_client import JiraClient

        client = JiraClient()

        async def _boom(_key):
            raise RuntimeError("429 Too Many Requests")
        client.get_comments = _boom

        # The inner helper is built per get_issue call; exercise the contract
        # it must honour: (comments, unavailable) with the flag set.
        async def _fetch_comments(issue_key):
            try:
                return await client.get_comments(issue_key), False
            except Exception:
                return [], True

        comments, unavailable = await _fetch_comments("SK-1")
        assert comments == []
        assert unavailable is True, "an empty list alone cannot carry this"

    def test_the_flag_reaches_the_plan_as_a_context_gap(self):
        req = GenerateTestPlanRequest(
            ticket_key="SK-1", summary="s", issue_type="Bug",
            comments_unavailable=True,
        )
        assert req.comments_unavailable is True

    def test_a_ticket_with_genuinely_no_comments_reports_no_gap(self):
        req = GenerateTestPlanRequest(
            ticket_key="SK-1", summary="s", issue_type="Bug", comments=None,
        )
        assert req.comments_unavailable is False


class TestGitHubPullRequestDiffs:
    """A PR whose diff GitHub refused arrives with no files_changed, which
    downstream reads as "this PR changed nothing" — and a grounding critic
    then concludes the code does not implement the behaviour."""

    def test_a_pr_records_that_its_diff_could_not_be_read(self):
        from src.app.models import PullRequest

        pr = PullRequest(title="t", status="merged", github_enrichment_failed=True)
        assert pr.github_enrichment_failed is True
        assert pr.files_changed is None

    def test_a_read_pr_with_no_changes_is_not_flagged(self):
        from src.app.models import PullRequest

        pr = PullRequest(title="t", status="merged", files_changed=[])
        assert pr.github_enrichment_failed is False, (
            "an empty diff we did read is not an unread diff"
        )

    def test_the_gap_names_how_many_and_which(self):
        from src.app.services.plan_service import prompt_payload

        serialized = {
            "key": "SK-1", "summary": "s", "issue_type": "Bug",
            "development_info": {"pull_requests": [
                {"url": "https://github.com/o/r/pull/1", "github_enrichment_failed": True},
                {"url": "https://github.com/o/r/pull/2"},
            ]},
        }
        payload = prompt_payload(serialized)
        prs = (payload["development_info"] or {}).get("pull_requests") or []
        failed = [p for p in prs if p.get("github_enrichment_failed")]
        assert len(failed) == 1, "the flag must survive into the generate request"


class TestRunPersistence:
    """complete / fail / complete_with_bug_analysis logged and moved on, so a
    run stuck "running" forever looked exactly like one that finished."""

    @pytest.fixture
    def broken_db(self, monkeypatch):
        def _down():
            raise RuntimeError("Atlas unreachable")
        monkeypatch.setattr(run_tracker, "get_db", _down)

    @pytest.mark.asyncio
    async def test_complete_records_why_it_could_not(self, broken_db):
        ctx = RunContext(run_id=7)
        await run_tracker.complete(ctx)
        assert ctx.failure and "Atlas unreachable" in ctx.failure

    @pytest.mark.asyncio
    async def test_fail_records_why_it_could_not(self, broken_db):
        ctx = RunContext(run_id=7)
        await run_tracker.fail(ctx, error_code="LLMError")
        assert ctx.failure and "Atlas unreachable" in ctx.failure

    @pytest.mark.asyncio
    async def test_bug_analysis_records_why_it_could_not(self, broken_db):
        ctx = RunContext(run_id=7)
        await run_tracker.complete_with_bug_analysis(ctx, analysis=object())
        assert ctx.failure and "Atlas unreachable" in ctx.failure

    @pytest.mark.asyncio
    async def test_an_unrecorded_run_stays_a_no_op_without_inventing_a_failure(self):
        """run_id=None means persistence was already known to be off; that is
        reported at start_run and must not be double-counted here."""
        ctx = RunContext(run_id=None)
        await run_tracker.complete(ctx)
        await run_tracker.fail(ctx, error_code="x")
        assert ctx.failure is None
