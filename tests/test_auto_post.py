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


def _merged(url="pr/1", merged_at="2026-10-15T00:00:00Z"):
    return {"url": url, "status": "MERGED", "merged_at": merged_at}


class _Harness:
    def __init__(
        self, *, rows=None, live=(), comments=(), post_result=None, post_error=None,
        prs=None, dev_unavailable=False,
    ):
        self.rows = [_row()] if rows is None else rows
        self.live = list(live)
        self.comments = list(comments)
        self.post_result = post_result or {"recorded": True, "comment_id": "c-1"}
        self.post_error = post_error
        self.jira = MagicMock()
        self.jira.get_comments = AsyncMock(return_value=self.comments)
        # The plan was written 2026-10-16; by default its only PR merged before.
        self.serialized = {
            "development_info": {"pull_requests": [_merged()] if prs is None else prs},
            "dev_status_unavailable": dev_unavailable,
        }
        self.jira.get_issue = AsyncMock(return_value=object())
        # What Mongo returns for a ticket's first-ever outcome.
        self.record = AsyncMock(return_value=None)
        self.add_event = AsyncMock()
        self.claim = AsyncMock(return_value=True)
        self.release = AsyncMock()

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
            patch.object(auto_post.auto_post_repository, "record", self.record),
            patch.object(auto_post.auto_post_repository, "add_event", self.add_event),
            patch.object(auto_post.auto_post_repository, "claim", self.claim),
            patch.object(auto_post.auto_post_repository, "release", self.release),
            patch.object(
                auto_post.plan_service, "serialize_issue", lambda _issue: self.serialized
            ),
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


def _live(plan_id=7, version=1, posted_at="2026-10-10T00:00:00+00:00", comment_id="c-live"):
    return (
        SimpleNamespace(ticket_keys=["SK-1"]),
        SimpleNamespace(
            id=plan_id, version=version, jira_comment_id=comment_id,
            posted_at=datetime.fromisoformat(posted_at),
        ),
    )


def _regenerated_row(plan_id=9, version=2, created_at="2026-10-16T00:00:00+00:00"):
    return {**_row(plan_id=plan_id), "version": version, "created_at": created_at}


def _single_marker():
    return patch.object(
        auto_post, "_comment_marker_kind", return_value=auto_post.PlanCommentKind.single
    )


async def test_the_live_version_itself_is_not_posted_again():
    with _Harness(live=[_live(plan_id=7)]) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.reason == auto_post.SKIP_ALREADY_LIVE
    h.post_one.assert_not_awaited()


async def test_a_regeneration_updates_the_live_comment_in_place():
    # SK-2835: plan 735 v1 live, plan 748 v2 generated after it went up. The
    # v2 auto-post was skipped as "already_live" and Jira kept v1.
    with _Harness(
        rows=[_regenerated_row(plan_id=748, version=2)],
        live=[_live(plan_id=735, version=1, comment_id="c-735")],
        post_result={"recorded": True, "comment_id": "c-735", "updated": True},
    ) as h, _single_marker():
        h.jira.get_comments.return_value = [{"id": "c-735"}]
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1", trigger=auto_post.TRIGGER_GENERATE)

    assert (out.action, out.reason) == (auto_post.POSTED, auto_post.POSTED_UPDATED)
    assert out.reason != auto_post.SKIP_ALREADY_LIVE
    assert "updated in place" in out.detail and "replaces v1, plan 735" in out.detail
    kwargs = h.post_one.await_args.kwargs
    # Same single-ticket slot: post_comment edits the comment(s) there,
    # including every part of a split plan, instead of adding one.
    assert kwargs["plan_id"] == 748 and kwargs["kind"] is auto_post.PlanCommentKind.single
    assert h.record.await_args.kwargs["outcome"]["reason"] == auto_post.POSTED_UPDATED


async def test_a_regeneration_with_its_comment_deleted_in_jira_is_posted_fresh():
    with _Harness(rows=[_regenerated_row()], live=[_live()]) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert (out.action, out.reason) == (auto_post.POSTED, auto_post.POSTED_UPDATED)


async def test_a_version_posted_after_the_newer_one_existed_is_a_choice_and_stays():
    # Someone re-posted v1 after v2 was generated: that is the plan they want up.
    with _Harness(
        rows=[_regenerated_row(created_at="2026-10-16T00:00:00+00:00")],
        live=[_live(posted_at="2026-10-17T00:00:00+00:00")],
    ) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.reason == auto_post.SKIP_ALREADY_LIVE
    assert out.detail.startswith("v2 not posted to Jira")
    h.post_one.assert_not_awaited()


async def test_a_regeneration_never_overwrites_a_comment_the_app_did_not_record_for_the_live_plan():
    with _Harness(rows=[_regenerated_row()], live=[_live(comment_id="c-live")]) as h, _single_marker():
        h.jira.get_comments.return_value = [{"id": "someone-elses"}]
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.reason == auto_post.SKIP_COMMENT_EXISTS
    assert "v2 not posted to Jira" in out.detail
    h.post_one.assert_not_awaited()


async def test_a_regeneration_held_back_by_a_check_says_which_version_jira_still_shows():
    with _Harness(
        rows=[_regenerated_row()], live=[_live()],
        prs=[_merged(merged_at="2026-10-17T00:00:00Z")],
    ) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.reason == auto_post.SKIP_STALE_PLAN
    assert out.detail.startswith("v2 not posted to Jira (Jira still shows v1, plan 7)")
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


async def test_every_real_outcome_is_recorded_for_the_plan_view():
    with _Harness(rows=[_row(case_count=0)]) as h:
        await auto_post.auto_post_own_plan(h.jira, "sk-1")
    kwargs = h.record.await_args.kwargs
    assert kwargs["ticket_key"] == "SK-1"
    assert kwargs["outcome"]["reason"] == auto_post.SKIP_EMPTY_PLAN
    assert kwargs["outcome"]["plan_id"] == 7


async def test_a_dry_run_records_nothing():
    with _Harness() as h:
        await auto_post.auto_post_own_plan(h.jira, "SK-1", dry_run=True)
    h.record.assert_not_awaited()


async def test_a_failure_keeps_the_plan_it_was_for():
    # Without the id the plan view cannot tell this failure belongs to the
    # plan on screen, and would not show it.
    with _Harness(post_error=RuntimeError("Jira is down")) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.plan_id == 7
    assert h.record.await_args.kwargs["outcome"]["plan_id"] == 7


async def test_a_recording_failure_does_not_change_the_outcome():
    with _Harness() as h:
        h.record.side_effect = RuntimeError("db down")
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.action == auto_post.POSTED


async def test_a_plan_older_than_its_merged_code_is_not_posted():
    late = _merged("pr/2", "2026-10-16T09:00:00Z")
    with _Harness(prs=[_merged(), late]) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert (out.action, out.reason) == (auto_post.SKIPPED, auto_post.SKIP_STALE_PLAN)
    assert "pr/2" in out.detail
    h.post_one.assert_not_awaited()


async def test_an_unreadable_development_panel_is_not_fresh():
    with _Harness(dev_unavailable=True) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert (out.action, out.reason) == (auto_post.FAILED, auto_post.FAIL_FRESHNESS_UNKNOWN)
    h.post_one.assert_not_awaited()


async def test_a_merged_pr_without_a_merge_time_is_not_fresh():
    with _Harness(prs=[_merged(merged_at=None)]) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.reason == auto_post.FAIL_FRESHNESS_UNKNOWN
    h.post_one.assert_not_awaited()


async def test_open_prs_do_not_make_a_plan_stale():
    open_pr = {"url": "pr/3", "status": "OPEN", "merged_at": None}
    with _Harness(prs=[_merged(), open_pr]) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.action == auto_post.POSTED


async def test_a_post_that_stopped_partway_is_not_a_success():
    result = {"recorded": True, "parts": 3, "posted_parts": 1, "part_error": "Jira 500"}
    with _Harness(post_result=result) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert (out.action, out.reason) == (auto_post.FAILED, "partial")
    assert "1 of 3" in out.detail and "Jira 500" in out.detail


async def test_a_truncated_post_is_not_a_success():
    with _Harness(post_result={"recorded": True, "truncated": True}) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert (out.action, out.reason) == (auto_post.FAILED, "truncated")


async def test_a_new_problem_alerts_once():
    alerts = []
    with _Harness(rows=[_row(case_count=0)]) as h, patch(
        "src.app.services.queue_watcher.notify", lambda t, m, **k: alerts.append((t, m))
    ):
        await auto_post.auto_post_own_plan(h.jira, "SK-1")
        # The next sweep finds the same problem with the same plan: no repeat.
        h.record.return_value = {"action": "skipped", "reason": "empty_plan", "plan_id": 7}
        await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert alerts == [("Test Plan Bot: SK-1 not posted", "The plan has no cases, so it was not posted.")]


async def test_a_problem_that_comes_back_alerts_again():
    alerts = []
    with _Harness(post_error=RuntimeError("Jira is down")) as h, patch(
        "src.app.services.queue_watcher.notify", lambda t, m, **k: alerts.append(m)
    ):
        h.record.return_value = {"action": "posted", "reason": None, "plan_id": 7}
        await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert len(alerts) == 1 and "Jira is down" in alerts[0]


async def test_normal_outcomes_never_alert():
    alerts = []
    with patch(
        "src.app.services.queue_watcher.notify", lambda *a, **k: alerts.append(a)
    ):
        with _Harness() as h:  # posted
            await auto_post.auto_post_own_plan(h.jira, "SK-1")
        with _Harness(rows=[]) as h:  # no plan of its own yet
            await auto_post.auto_post_own_plan(h.jira, "SK-1")
        with _Harness() as h:  # dry run records nothing, alerts nothing
            await auto_post.auto_post_own_plan(h.jira, "SK-1", dry_run=True)
    assert alerts == []


async def test_a_stale_plan_alerts_with_the_way_out():
    alerts = []
    late = _merged("pr/2", "2026-10-16T09:00:00Z")
    with _Harness(prs=[late]) as h, patch(
        "src.app.services.queue_watcher.notify", lambda t, m, **k: alerts.append(m)
    ):
        await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert len(alerts) == 1 and "Regenerate" in alerts[0]


async def test_an_alert_that_cannot_be_shown_changes_nothing():
    def _boom(*a, **k):
        raise RuntimeError("no notification centre")
    with _Harness(rows=[_row(case_count=0)]) as h, patch(
        "src.app.services.queue_watcher.notify", _boom
    ):
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.reason == auto_post.SKIP_EMPTY_PLAN


async def test_a_changed_outcome_is_logged_with_its_trigger():
    with _Harness() as h:
        await auto_post.auto_post_own_plan(h.jira, "SK-1", trigger=auto_post.TRIGGER_PULL)
    event = h.add_event.await_args.kwargs["event"]
    assert (event["action"], event["trigger"], event["plan_id"]) == (
        auto_post.POSTED, auto_post.TRIGGER_PULL, 7,
    )


async def test_a_repeated_outcome_is_not_logged_again():
    # Every sweep re-checks the ticket; the history must not fill with copies.
    with _Harness(rows=[_row(case_count=0)]) as h:
        h.record.return_value = {"action": "skipped", "reason": "empty_plan", "plan_id": 7}
        await auto_post.auto_post_own_plan(h.jira, "SK-1")
    h.add_event.assert_not_awaited()


async def test_manual_posts_are_logged_and_never_raise():
    add = AsyncMock()
    with patch.object(auto_post, "get_db", noop_get_db), \
         patch.object(auto_post.auto_post_repository, "add_event", add):
        await auto_post.log_manual_post("sk-1", {"plan_id": 9, "updated": True})
        await auto_post.log_manual_post("sk-1", {"plan_id": None})
        add.side_effect = RuntimeError("db down")
        await auto_post.log_manual_post("sk-1", {"plan_id": 9})
    first = add.await_args_list[0].kwargs
    assert first["ticket_key"] == "sk-1"
    assert first["event"]["trigger"] == auto_post.TRIGGER_MANUAL
    assert first["event"]["detail"] == "updated"
    assert add.await_count == 2  # the plan-less post was not logged


# --- the board sweep --------------------------------------------------------


async def test_sweep_asks_only_for_tickets_that_entered_the_status(monkeypatch):
    monkeypatch.setattr(settings, "auto_post_status", "In Testing")
    monkeypatch.setattr(settings, "auto_post_lookback_hours", 24)
    search = AsyncMock(return_value=["SK-1"])
    post = AsyncMock(return_value=auto_post.AutoPostOutcome("SK-1", auto_post.POSTED))
    with patch.object(queue_watcher.JiraClient, "search_keys_entered_status", search), \
         patch.object(queue_watcher.auto_post, "auto_post_own_plan", post):
        result = await queue_watcher.sweep_auto_post(projects=["SK"])

    search.assert_awaited_once_with(
        "SK", "In Testing", within_hours=24,
        settle_minutes=queue_watcher.AUTO_POST_SETTLE_MINUTES,
    )
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


async def test_the_search_excludes_old_and_fresh_moves_in_jql():
    """The JQL is the backfill guard and the duplicate guard both. `DURING`
    was tried and rejected: it rounds relative bounds and matched a move made
    before its own start, so the exclusion has to be `NOT ... AFTER`."""
    from src.app import jira_client as jc

    sent = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"issues": [{"key": "SK-1"}]}

        def raise_for_status(self):
            pass

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            sent["jql"] = json["jql"]
            return _Resp()

    with patch.object(jc, "retrying_client", lambda **_: _Client()):
        keys = await jc.JiraClient().search_keys_entered_status(
            "SK", "In Testing", within_hours=24, settle_minutes=2
        )

    assert keys == ["SK-1"]
    assert sent["jql"] == (
        'project = SK AND status = "In Testing" '
        'AND status CHANGED TO "In Testing" AFTER -24h '
        'AND NOT status CHANGED TO "In Testing" AFTER -2m '
        "ORDER BY updated DESC"
    )
    assert "DURING" not in sent["jql"]


# --- posting on the QA queue (AUTO_POST_ON_QUEUE) ---------------------------


def _queue_on(monkeypatch, on=True):
    monkeypatch.setattr(settings, "auto_post_status", "In Testing")
    monkeypatch.setattr(settings, "watch_status", "Ready to Test")
    monkeypatch.setattr(settings, "auto_post_on_queue", on)
    monkeypatch.setattr(settings, "auto_post_lookback_hours", 24)


async def test_posting_statuses_add_the_queue_only_when_switched_on(monkeypatch):
    _queue_on(monkeypatch, on=False)
    assert auto_post.posting_statuses() == ["In Testing"]
    assert not auto_post.posts_on("Ready To Test")
    _queue_on(monkeypatch)
    assert auto_post.posting_statuses() == ["In Testing", "Ready to Test"]
    assert auto_post.posts_on("ready to test") and auto_post.posts_on("In Testing")
    assert not auto_post.posts_on("Ready for UAT") and not auto_post.posts_on(None)
    # Auto-post switched off is off for the queue too.
    monkeypatch.setattr(settings, "auto_post_status", "")
    assert auto_post.posting_statuses() == []


async def test_sweep_also_posts_tickets_that_entered_the_queue(monkeypatch):
    _queue_on(monkeypatch)
    monkeypatch.setattr(settings, "uat_auto_queue", True)

    async def search(project, status, **_kw):
        return {"In Testing": ["SK-1"], "Ready to Test": ["SK-2"]}[status]

    post = AsyncMock(side_effect=lambda _j, key, **_kw: auto_post.AutoPostOutcome(key, auto_post.POSTED))
    uat = AsyncMock()
    with patch.object(queue_watcher.JiraClient, "search_keys_entered_status", AsyncMock(side_effect=search)) as s, \
         patch.object(queue_watcher.auto_post, "auto_post_own_plan", post), \
         patch.object(queue_watcher, "_queue_uat_run", uat):
        result = await queue_watcher.sweep_auto_post(projects=["SK"])

    assert [c.args[1] for c in s.await_args_list] == ["In Testing", "Ready to Test"]
    assert [o.ticket_key for o in result.outcomes] == ["SK-1", "SK-2"]
    # A UAT run is for a ticket being tested, not one still in the queue.
    assert [c.args[0] for c in uat.await_args_list] == ["SK-1"]


async def test_sweep_leaves_the_queue_alone_when_switched_off(monkeypatch):
    _queue_on(monkeypatch, on=False)
    search = AsyncMock(return_value=[])
    with patch.object(queue_watcher.JiraClient, "search_keys_entered_status", search):
        await queue_watcher.sweep_auto_post(projects=["SK"])
    assert [c.args[1] for c in search.await_args_list] == ["In Testing"]


async def test_one_unreadable_status_does_not_hide_the_other(monkeypatch):
    _queue_on(monkeypatch)

    async def search(project, status, **_kw):
        if status == "In Testing":
            raise queue_watcher.JiraConnectionError("offline")
        return ["SK-2"]

    post = AsyncMock(return_value=auto_post.AutoPostOutcome("SK-2", auto_post.POSTED))
    with patch.object(queue_watcher.JiraClient, "search_keys_entered_status", AsyncMock(side_effect=search)), \
         patch.object(queue_watcher.auto_post, "auto_post_own_plan", post):
        result = await queue_watcher.sweep_auto_post(projects=["SK"])
    assert "offline" in result.queue_error
    assert [o.ticket_key for o in result.outcomes] == ["SK-2"]


async def test_a_ticket_found_under_both_statuses_is_posted_once(monkeypatch):
    _queue_on(monkeypatch)
    post = AsyncMock(return_value=auto_post.AutoPostOutcome("SK-1", auto_post.POSTED))
    with patch.object(queue_watcher.JiraClient, "search_keys_entered_status", AsyncMock(return_value=["SK-1"])), \
         patch.object(queue_watcher.auto_post, "auto_post_own_plan", post):
        await queue_watcher.sweep_auto_post(projects=["SK"])
    post.assert_awaited_once()


# --- one poster at a time across processes ----------------------------------


async def test_a_post_holds_the_ticket_lease_and_gives_it_back():
    with _Harness() as h:
        out = await auto_post.auto_post_own_plan(h.jira, "sk-1")
    assert out.action == auto_post.POSTED
    claimed = h.claim.await_args.kwargs
    assert claimed["ticket_key"] == "SK-1"
    assert claimed["lease_seconds"] == auto_post.CLAIM_LEASE_SECONDS
    assert h.release.await_args.kwargs == {"ticket_key": "SK-1", "owner": claimed["owner"]}


async def test_a_ticket_another_process_is_posting_is_skipped_and_not_recorded():
    with _Harness() as h:
        h.claim.return_value = False
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert (out.action, out.reason) == (auto_post.SKIPPED, auto_post.SKIP_POST_IN_PROGRESS)
    h.post_one.assert_not_awaited()
    h.jira.get_comments.assert_not_awaited()
    # The holder records its own outcome; this one must not race it.
    h.record.assert_not_awaited()
    h.release.assert_not_awaited()


async def test_no_claim_no_post():
    with _Harness() as h:
        h.claim.side_effect = RuntimeError("db down")
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert (out.action, out.reason) == (auto_post.FAILED, "claim_failed")
    h.post_one.assert_not_awaited()


async def test_the_lease_is_given_back_when_the_post_fails():
    with _Harness(post_error=RuntimeError("Jira is down")) as h:
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.action == auto_post.FAILED
    h.release.assert_awaited_once()


async def test_a_failed_release_does_not_change_the_outcome():
    with _Harness() as h:
        h.release.side_effect = RuntimeError("db blip")
        out = await auto_post.auto_post_own_plan(h.jira, "SK-1")
    assert out.action == auto_post.POSTED


async def test_a_dry_run_takes_no_lease():
    with _Harness() as h:
        await auto_post.auto_post_own_plan(h.jira, "SK-1", dry_run=True)
    h.claim.assert_not_awaited()
    h.release.assert_not_awaited()
