"""The watcher's working hours, and the three things that must agree on them:
the sweep, the auto-post lookback, and /health/watcher."""
import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from src.app import main, watch_schedule as ws
from src.app.config import settings
from src.app.repositories import watcher_heartbeat_repository as repo
from src.cli.main import app as cli

from .conftest import noop_get_db

TZ = ZoneInfo("America/Los_Angeles")


def at(day, hour, minute=0):
    # 2026-10-19 is a Monday.
    return datetime(2026, 10, 19 + day, hour, minute, tzinfo=TZ)


@pytest.fixture(autouse=True)
def _local_zone_is_la(monkeypatch):
    # The schedule reads the machine's local zone; CI runs in UTC.
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def weekdays(monkeypatch):
    monkeypatch.setattr(settings, "watch_active_days", "mon-fri")
    monkeypatch.setattr(settings, "watch_active_hours", "07:00-18:00")


def test_no_schedule_means_always():
    assert ws.is_active(at(5, 3)) is True
    assert ws.lookback_hours(24, at(5, 3)) == 24
    assert ws.describe() == "always"


def test_weekday_hours(weekdays):
    assert ws.is_active(at(0, 7)) is True
    assert ws.is_active(at(0, 17, 59)) is True
    assert ws.is_active(at(0, 18)) is False  # the end is exclusive
    assert ws.is_active(at(0, 6, 59)) is False
    assert ws.is_active(at(5, 12)) is False  # Saturday


def test_monday_morning_looks_back_across_the_weekend(weekdays):
    # Friday 18:00 to Monday 07:00 is 61h; a 24h lookback would drop a
    # ticket dragged into testing on Friday night.
    assert ws.lookback_hours(24, at(0, 7)) == 63
    # Still covered if the 07:00 sweep failed and 09:00 is the first good one.
    assert ws.lookback_hours(24, at(0, 9)) == 65


def test_an_ordinary_weekday_keeps_the_configured_lookback(weekdays):
    assert ws.lookback_hours(24, at(1, 10)) == 24


def test_day_ranges_can_wrap(monkeypatch):
    monkeypatch.setattr(settings, "watch_active_days", "sat-mon")
    assert {d for d in range(7) if ws.is_active(at(d, 12))} == {0, 5, 6}


@pytest.mark.parametrize("days,hours", [("mon-funday", ""), ("", "7am-6pm"), ("", "18:00-07:00")])
def test_a_bad_schedule_is_an_error_not_a_guess(monkeypatch, days, hours):
    monkeypatch.setattr(settings, "watch_active_days", days)
    monkeypatch.setattr(settings, "watch_active_hours", hours)
    with pytest.raises(ws.ScheduleError):
        ws.is_active(at(0, 9))


# --- /health/watcher ---------------------------------------------------------

client = TestClient(main.app)


def _health(beat_at, now):
    beat = {"at": beat_at, "interval_seconds": 300, "queue_error": None}
    with patch.object(main, "get_db", noop_get_db), \
         patch.object(repo, "get", AsyncMock(return_value=beat)), \
         patch.object(ws, "current_time", lambda: now):
        return client.get("/health/watcher").json()


def test_off_hours_is_paused_not_stopped(weekdays):
    assert _health(at(-3, 17, 55), now=at(-2, 12))["state"] == "paused"  # Saturday


def test_just_after_opening_is_not_late(weekdays):
    # Last sweep Friday 17:55, Monday 07:05: due since 07:00, not since Friday.
    body = _health(at(-3, 17, 55).astimezone(timezone.utc), now=at(0, 7, 5))
    assert body["state"] == "ok"


def test_no_sweep_well_after_opening_is_late(weekdays):
    body = _health(at(-3, 17, 55).astimezone(timezone.utc), now=at(0, 7, 20))
    assert body["state"] == "late"


# --- the sweep ---------------------------------------------------------------

runner = CliRunner()


def _cli(args, *, active):
    from src.app.services import queue_watcher

    sweep = AsyncMock(return_value=queue_watcher.SweepResult())
    with patch("src.cli.commands.watch_cmd.config_manager") as cfg, \
         patch("src.cli.commands.watch_cmd._apply_config_env"), \
         patch.object(ws, "is_active", lambda now=None: active), \
         patch("src.cli.commands.watch_cmd.queue_watcher.sweep_once", sweep), \
         patch("src.cli.commands.watch_cmd.queue_watcher.check_tokens_if_due", AsyncMock()):
        cfg.is_configured.return_value = True
        cfg.load.return_value.jira_url = ""
        result = runner.invoke(cli, ["watch", "--project", "SK", *args])
    return result, sweep


def test_outside_hours_the_sweep_does_nothing():
    result, sweep = _cli(["--once"], active=False)
    assert result.exit_code == 0, result.output
    assert "Outside active hours" in result.output
    sweep.assert_not_awaited()


def test_anytime_overrides_the_schedule():
    from src.app.services import queue_watcher

    posted = AsyncMock(return_value=queue_watcher.AutoPostSweepResult())
    with patch("src.cli.commands.watch_cmd.queue_watcher.sweep_auto_post", posted), \
         patch("src.cli.commands.watch_cmd.queue_watcher.record_heartbeat", AsyncMock()), \
         patch("src.cli.commands.watch_cmd._render"), \
         patch("src.cli.commands.watch_cmd._render_auto_post"):
        result, sweep = _cli(["--once", "--anytime"], active=False)
    assert result.exit_code == 0, result.output
    sweep.assert_awaited_once()


def test_a_bad_schedule_fails_the_sweep_loudly(monkeypatch):
    monkeypatch.setattr(settings, "watch_active_hours", "nonsense")
    with patch("src.cli.commands.watch_cmd.config_manager") as cfg, \
         patch("src.cli.commands.watch_cmd._apply_config_env"):
        cfg.is_configured.return_value = True
        cfg.load.return_value.jira_url = ""
        result = runner.invoke(cli, ["watch", "--project", "SK", "--once"])
    assert result.exit_code == 1
    assert "Bad watcher schedule" in result.output
