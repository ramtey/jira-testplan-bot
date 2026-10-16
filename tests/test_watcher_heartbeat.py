"""The watcher's heartbeat and /health/watcher.

The watcher dying is silent by nature — nothing errors, plans just stop
arriving — so what's pinned is that each way of being unhealthy reads as
such, and that "could not look" is never reported as healthy.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.app import main
from src.app.repositories import watcher_heartbeat_repository as repo
from src.app.services import auto_post, queue_watcher

from .conftest import noop_get_db

client = TestClient(main.app)


def _health(beat=None, error=None):
    with patch.object(main, "get_db", noop_get_db), patch.object(
        repo, "get", AsyncMock(return_value=beat, side_effect=error)
    ):
        return client.get("/health/watcher")


def _beat(minutes_ago, **over):
    return {
        "at": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        "interval_seconds": 300,
        "queue_error": None,
        **over,
    }


def test_a_watcher_never_set_up_is_not_an_alarm():
    assert _health(None).json()["state"] == "never"


def test_a_recent_clean_sweep_is_ok():
    assert _health(_beat(4)).json()["state"] == "ok"


def test_three_missed_intervals_is_late():
    body = _health(_beat(16)).json()
    assert body["state"] == "late"
    assert body["minutes_since"] == 16


def test_late_wins_over_an_old_error():
    assert _health(_beat(60, queue_error="x")).json()["state"] == "late"


def test_a_recent_sweep_that_could_not_read_jira_is_an_error():
    body = _health(_beat(2, queue_error="JiraAuthError: expired")).json()
    assert body["state"] == "error"
    assert "expired" in body["queue_error"]


def test_an_unreadable_store_is_503_not_healthy():
    assert _health(error=RuntimeError("db down")).status_code == 503


@pytest.mark.asyncio
async def test_the_heartbeat_counts_both_sweeps_and_joins_their_errors():
    result = queue_watcher.SweepResult(
        outcomes=[
            queue_watcher.TicketOutcome("SK-1", "generated"),
            queue_watcher.TicketOutcome("SK-2", "failed", "fetch_failed"),
        ],
        queue_error="board unreadable",
    )
    posted = queue_watcher.AutoPostSweepResult(
        outcomes=[
            auto_post.AutoPostOutcome("SK-3", auto_post.POSTED),
            auto_post.AutoPostOutcome("SK-4", auto_post.FAILED, "post_failed"),
        ],
        queue_error="status unreadable",
    )
    record = AsyncMock()
    with patch.object(queue_watcher, "get_db", noop_get_db), patch.object(repo, "record", record):
        await queue_watcher.record_heartbeat(result, posted, interval_seconds=300)
    kwargs = record.await_args.kwargs
    assert kwargs["queue_error"] == "board unreadable; status unreadable"
    assert (kwargs["generated"], kwargs["posted"], kwargs["failed"]) == (1, 1, 2)
    assert kwargs["interval_seconds"] == 300


@pytest.mark.asyncio
async def test_a_heartbeat_that_cannot_be_written_does_not_fail_the_sweep():
    with patch.object(queue_watcher, "get_db", noop_get_db), patch.object(
        repo, "record", AsyncMock(side_effect=RuntimeError("db down"))
    ):
        await queue_watcher.record_heartbeat(
            queue_watcher.SweepResult(), queue_watcher.AutoPostSweepResult(), interval_seconds=300
        )
