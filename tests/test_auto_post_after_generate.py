"""A plan generated on a ticket already in testing is posted without waiting
for the sweep — the gap Pull to testing left when it ran before any plan
existed. Gated on the ticket's real status, read from Jira, never on what the
browser says it is."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from src.app import main
from src.app.config import settings
from src.app.services import auto_post

PLAN = {"plan_id": 7, "happy_path": [{"title": "t"}]}


def _jira(status="In Testing", error=None):
    jira = MagicMock()
    jira.get_issue_status = AsyncMock(return_value=status, side_effect=error)
    return jira


async def _queue(plan=PLAN, **jira_kwargs):
    tasks = BackgroundTasks()
    with patch.object(auto_post, "JiraClient", return_value=_jira(**jira_kwargs)):
        out = await auto_post.queue_if_in_testing(tasks, "sk-1", plan)
    return out, tasks.tasks


@pytest.mark.asyncio
async def test_queues_the_post_on_a_ticket_in_testing(monkeypatch):
    monkeypatch.setattr(settings, "auto_post_status", "In Testing")
    out, tasks = await _queue()
    assert out == {"action": "queued"}
    assert [t.func for t in tasks] == [auto_post.auto_post_own_plan]
    assert tasks[0].args[1] == "SK-1"
    assert tasks[0].kwargs == {"trigger": auto_post.TRIGGER_GENERATE}


@pytest.mark.asyncio
async def test_a_ticket_not_in_testing_is_left_alone_but_logged(monkeypatch):
    monkeypatch.setattr(settings, "auto_post_status", "In Testing")
    with patch.object(auto_post.auto_post_repository, "add_event", AsyncMock()) as add, \
         patch.object(auto_post, "get_db"):
        out, tasks = await _queue(status="Ready to Test")
    assert out is None and tasks == []
    event = add.await_args.kwargs["event"]
    assert add.await_args.kwargs["ticket_key"] == "SK-1"
    assert event["reason"] == auto_post.SKIP_NOT_IN_TESTING
    assert event["trigger"] == auto_post.TRIGGER_GENERATE
    assert event["plan_id"] == 7
    assert "Ready to Test" in event["detail"]


@pytest.mark.asyncio
async def test_a_failed_history_write_still_returns(monkeypatch):
    monkeypatch.setattr(settings, "auto_post_status", "In Testing")
    with patch.object(auto_post.auto_post_repository, "add_event", AsyncMock(side_effect=RuntimeError("db down"))), \
         patch.object(auto_post, "get_db"):
        out, tasks = await _queue(status="Ready to Test")
    assert out is None and tasks == []


@pytest.mark.asyncio
async def test_a_watcher_plan_waiting_for_testing_is_logged(monkeypatch):
    monkeypatch.setattr(settings, "auto_post_status", "In Testing")
    with patch.object(auto_post, "JiraClient", return_value=_jira(status="Ready To Test")), \
         patch.object(auto_post.auto_post_repository, "add_event", AsyncMock()) as add, \
         patch.object(auto_post, "get_db"):
        await auto_post.note_watcher_plan("sk-1", PLAN)
    assert add.await_args.kwargs["event"]["trigger"] == auto_post.TRIGGER_WATCHER


@pytest.mark.asyncio
async def test_a_watcher_plan_on_a_ticket_already_in_testing_is_left_to_the_sweep(monkeypatch):
    monkeypatch.setattr(settings, "auto_post_status", "In Testing")
    with patch.object(auto_post, "JiraClient", return_value=_jira()), \
         patch.object(auto_post.auto_post_repository, "add_event", AsyncMock()) as add:
        await auto_post.note_watcher_plan("sk-1", PLAN)
        await auto_post.note_watcher_plan("sk-1", {"plan_id": 7, "no_source": True})
    add.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unreadable_status_queues_nothing(monkeypatch):
    monkeypatch.setattr(settings, "auto_post_status", "In Testing")
    out, tasks = await _queue(error=RuntimeError("Jira down"))
    assert out is None and tasks == []


@pytest.mark.asyncio
async def test_an_unsaved_or_no_source_plan_is_never_queued(monkeypatch):
    monkeypatch.setattr(settings, "auto_post_status", "In Testing")
    assert (await _queue(plan={"happy_path": []}))[0] is None
    assert (await _queue(plan={"plan_id": 7, "no_source": True}))[0] is None


@pytest.mark.asyncio
async def test_switched_off_means_off(monkeypatch):
    monkeypatch.setattr(settings, "auto_post_status", "")
    out, tasks = await _queue()
    assert out is None and tasks == []


def test_the_generate_response_says_the_post_is_queued():
    client = TestClient(main.app)
    body = {"ticket_key": "SK-1", "summary": "s", "issue_type": "Story"}
    with patch.object(main.plan_service, "generate_single", AsyncMock(return_value=dict(PLAN))), \
         patch.object(main.auto_post, "queue_if_in_testing", AsyncMock(return_value={"action": "queued"})) as q:
        response = client.post("/generate-test-plan", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["auto_post"] == {"action": "queued"}
    assert q.await_args.args[1] == "SK-1"


def test_a_batch_plan_from_the_key_door_is_never_queued():
    client = TestClient(main.app)
    with patch.object(main.plan_service, "generate_for_tickets", AsyncMock(return_value=dict(PLAN))), \
         patch.object(main.auto_post, "queue_if_in_testing", AsyncMock()) as q:
        response = client.post("/tickets/SK-1,SK-2/plan")
    assert response.status_code == 200, response.text
    assert "auto_post" not in response.json()
    q.assert_not_awaited()


# --- AUTO_POST_ON_QUEUE: the watcher posts what it generated -----------------


def _queue_on(monkeypatch, on=True):
    monkeypatch.setattr(settings, "auto_post_status", "In Testing")
    monkeypatch.setattr(settings, "watch_status", "Ready to Test")
    monkeypatch.setattr(settings, "auto_post_on_queue", on)


@pytest.mark.asyncio
async def test_a_watcher_plan_on_a_queued_ticket_is_posted_at_once(monkeypatch):
    _queue_on(monkeypatch)
    outcome = auto_post.AutoPostOutcome("SK-1", auto_post.POSTED, plan_id=7)
    with patch.object(auto_post, "JiraClient", return_value=_jira(status="Ready To Test")), \
         patch.object(auto_post, "auto_post_own_plan", AsyncMock(return_value=outcome)) as post, \
         patch.object(auto_post.auto_post_repository, "add_event", AsyncMock()) as add:
        got = await auto_post.note_watcher_plan("sk-1", PLAN)
    assert got is outcome
    assert post.await_args.args[1] == "SK-1"
    assert post.await_args.kwargs == {"trigger": auto_post.TRIGGER_WATCHER}
    add.assert_not_awaited()  # nothing to log as waiting


@pytest.mark.asyncio
async def test_a_no_source_or_unsaved_watcher_plan_posts_nothing(monkeypatch):
    _queue_on(monkeypatch)
    with patch.object(auto_post, "JiraClient", return_value=_jira(status="Ready to Test")) as client, \
         patch.object(auto_post, "auto_post_own_plan", AsyncMock()) as post:
        assert await auto_post.note_watcher_plan("sk-1", {"plan_id": 7, "no_source": True}) is None
        assert await auto_post.note_watcher_plan("sk-1", {"happy_path": []}) is None
    post.assert_not_awaited()
    client.assert_not_called()


@pytest.mark.asyncio
async def test_queue_posting_off_still_only_logs_the_wait(monkeypatch):
    _queue_on(monkeypatch, on=False)
    with patch.object(auto_post, "JiraClient", return_value=_jira(status="Ready to Test")), \
         patch.object(auto_post, "auto_post_own_plan", AsyncMock()) as post, \
         patch.object(auto_post.auto_post_repository, "add_event", AsyncMock()) as add, \
         patch.object(auto_post, "get_db"):
        assert await auto_post.note_watcher_plan("sk-1", PLAN) is None
    post.assert_not_awaited()
    assert add.await_args.kwargs["event"]["reason"] == auto_post.SKIP_NOT_IN_TESTING


@pytest.mark.asyncio
async def test_a_watcher_plan_on_a_ticket_pulled_meanwhile_is_still_left_to_the_sweep(monkeypatch):
    """Pull to testing posts on its own; posting here too could race it."""
    _queue_on(monkeypatch)
    with patch.object(auto_post, "JiraClient", return_value=_jira(status="In Testing")), \
         patch.object(auto_post, "auto_post_own_plan", AsyncMock()) as post, \
         patch.object(auto_post.auto_post_repository, "add_event", AsyncMock()) as add:
        assert await auto_post.note_watcher_plan("sk-1", PLAN) is None
    post.assert_not_awaited()
    add.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_plan_generated_by_hand_on_a_queued_ticket_is_queued(monkeypatch):
    _queue_on(monkeypatch)
    out, tasks = await _queue(status="Ready to Test")
    assert out == {"action": "queued"}
    assert [t.func for t in tasks] == [auto_post.auto_post_own_plan]
