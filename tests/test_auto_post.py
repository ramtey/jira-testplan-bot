"""Auto-posting a plan when its ticket enters testing.

Nobody is watching when this posts, so what's pinned here is mostly the
refusals — every case where posting would undo a person's choice or put a
failure on the ticket looking like a plan — and that no outcome is ever a
raise or a silent success.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio

from src.app.config import settings
from src.app.services import auto_post, queue_watcher

from .conftest import noop_get_db

PLAN_BODY = {
    "happy_path": [
        {"title": "Saving a draft keeps it", "steps": ["Open", "Save"], "expected_result": "Kept"}
    ],
}


def _row(plan_id=7, keys=("SK-1",), case_count=1):
    return {"plan_id": plan_id, "ticket_keys": list(keys), "case_count": case_count}


def _stored(plan_id=7):
    return SimpleNamespace(
        id=plan_id,
        version=1,
        body=json.dumps(PLAN_BODY),
        created_at=datetime(2026, 10, 16, tzinfo=timezone.utc),
    )


class _Harness:
    def __init__(self, *, rows=None, live=(), comments=(), post_result=None, post_error=None):
        self.rows = [_row()] if rows is None else rows
        self.live = list(live)
        self.comments = list(comments)
        self.post_result = post_result or {"recorded": True, "comment_id": "c-1"}
        self.post_error = post_error
        self.jira = MagicMock()
        self.jira.get_comments = AsyncMock(return_value=self.comments)

    def __enter__(self):
        repo = auto_post.plan_repository
        self.post_one = AsyncMock(
            side_effect=self.post_error, return_value=self.post_result
        )
        self._patches = [
            patch.object(auto_post, "get_db", noop_get_db),
            patch.object(repo, "list_runs_with_plans_by_ticket", AsyncMock(return_value=self.rows)),
            patch.object(repo, "list_live_plans_for_ticket", AsyncMock(return_value=self.live)),
            patch.object(repo, "get_plan_with_cases", AsyncMock(return_value=(_stored(), []))),
            patch.object(auto_post.uat_readiness, "fetch_readiness", AsyncMock(return_value={})),
            patch.object(auto_post.plan_posting, "post_one", self.post_one),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


async def test_posts_the_plan_through_the_browser_formatter():
    with _Harness() as h:
        out = await auto_post.auto_post_own_plan(h.jira, "sk-1")

    assert out.action == auto_post.POSTED
    kwargs = h.post_one.await_args.kwargs
    assert kwargs["issue_key"] == "SK-1"
    assert kwargs["plan_id"] == 7
    assert kwargs["kind"] is auto_post.PlanCommentKind.single
    # Real node, real formatTestPlanAsJira: the case id and title are what
    # the Post to Jira button would have written.
    assert "[happy_path:0] Saving a draft keeps it" in kwargs["comment_text"]


async def test_a_batch_only_ticket_is_left_to_the_tester():
    with _Harness(rows=[_row(keys=("SK-1", "SK-2"))]) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert (out.action, out.reason) == (auto_post.SKIPPED, auto_post.SKIP_NO_OWN_PLAN)
    h.post_one.assert_not_awaited()


async def test_a_live_plan_is_never_replaced():
    # A newer unposted regeneration behind a live plan is the tester's choice.
    run = SimpleNamespace(ticket_keys=["SK-1"])
    with _Harness(rows=[_row(plan_id=9)], live=[(run, SimpleNamespace(id=7))]) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.reason == auto_post.SKIP_ALREADY_LIVE
    h.post_one.assert_not_awaited()


async def test_a_live_batch_plan_does_not_block_the_own_plan():
    run = SimpleNamespace(ticket_keys=["SK-1", "SK-2"])
    with _Harness(live=[(run, SimpleNamespace(id=3))]) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.action == auto_post.POSTED


async def test_an_empty_plan_is_not_posted():
    with _Harness(rows=[_row(case_count=0)]) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.reason == auto_post.SKIP_EMPTY_PLAN
    h.post_one.assert_not_awaited()


async def test_a_plan_comment_the_store_missed_is_not_overwritten():
    with _Harness() as h, patch.object(
        auto_post, "_comment_marker_kind", return_value=auto_post.PlanCommentKind.single
    ):
        h.jira.get_comments.return_value = [{"id": "old"}]
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.reason == auto_post.SKIP_COMMENT_EXISTS
    h.post_one.assert_not_awaited()


async def test_dry_run_checks_everything_and_posts_nothing():
    with _Harness() as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1", dry_run=True)
    assert out.reason == "dry_run"
    h.post_one.assert_not_awaited()


async def test_a_jira_failure_is_reported_not_raised():
    with _Harness(post_error=RuntimeError("Jira is down")) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.action == auto_post.FAILED
    assert "Jira is down" in out.detail


async def test_a_post_the_store_did_not_record_is_not_a_success():
    with _Harness(post_result={"recorded": False, "record_error": "db down"}) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert (out.action, out.reason) == (auto_post.FAILED, "not_recorded")


async def test_no_node_is_a_failure_with_a_reason():
    with _Harness() as h, patch.object(auto_post.shutil, "which", return_value=None):
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.action == auto_post.FAILED
    assert "node" in out.detail
    h.post_one.assert_not_awaited()


# --- the board sweep --------------------------------------------------------


async def test_sweep_asks_only_for_tickets_that_entered_the_status(monkeypatch):
    monkeypatch.setattr(settings, "auto_post_status", "In Testing")
    monkeypatch.setattr(settings, "auto_post_lookback_hours", 24)
    search = AsyncMock(return_value=["SK-1"])
    post = AsyncMock(return_value=auto_post.AutoPostOutcome("SK-1", auto_post.POSTED))
    with patch.object(queue_watcher.JiraClient, "search_keys_entered_status", search), \
         patch.object(queue_watcher.auto_post, "auto_post_own_plan", post):
        result = await queue_watcher.sweep_auto_post(projects=["SK"])

    search.assert_awaited_once_with("SK", "In Testing", within_hours=24)
    assert [o.action for o in result.outcomes] == [auto_post.POSTED]


async def test_sweep_is_off_when_no_status_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "auto_post_status", "")
    search = AsyncMock()
    with patch.object(queue_watcher.JiraClient, "search_keys_entered_status", search):
        result = await queue_watcher.sweep_auto_post(projects=["SK"])
    search.assert_not_awaited()
    assert result.outcomes == []


async def test_an_unreadable_board_is_not_an_empty_one(monkeypatch):
    monkeypatch.setattr(settings, "auto_post_status", "In Testing")
    search = AsyncMock(side_effect=queue_watcher.JiraConnectionError("offline"))
    with patch.object(queue_watcher.JiraClient, "search_keys_entered_status", search):
        result = await queue_watcher.sweep_auto_post(projects=["SK"])
    assert "offline" in result.queue_error
