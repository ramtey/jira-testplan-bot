"""`testplan auto-post`: the numbers the next automation step is chosen from."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from src.cli.commands import auto_post_cmd
from src.cli.main import app

runner = CliRunner()
AT = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def _run(events=(), versions=None, error=None):
    load = AsyncMock(return_value=(list(events), versions or {}), side_effect=error)
    with patch.object(auto_post_cmd, "_load", load):
        return runner.invoke(app, ["auto-post", "--days", "7"])


def test_summarises_posts_skips_failures_and_manual_posts():
    events = [
        {"ticket_key": "SK-1", "at": AT, "action": "posted", "trigger": "pull_to_testing", "plan_id": 1},
        {"ticket_key": "SK-2", "at": AT, "action": "posted", "trigger": "sweep", "plan_id": 2},
        {"ticket_key": "SK-3", "at": AT, "action": "skipped", "reason": "stale_plan", "trigger": "sweep", "plan_id": 3},
        {"ticket_key": "SK-4", "at": AT, "action": "skipped", "reason": "no_own_plan", "trigger": "sweep"},
        {"ticket_key": "SK-5", "at": AT, "action": "failed", "reason": "post_failed", "trigger": "generate", "plan_id": 5},
        {"ticket_key": "SK-3", "at": AT, "action": "posted", "trigger": "manual", "plan_id": 6, "detail": "updated"},
    ]
    result = _run(events, versions={1: 1, 2: 1, 3: 1, 5: 1, 6: 2})
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Posted automatically: 2" in out
    assert "stale_plan 1" in out
    # "No plan yet" is the normal state of a fresh ticket, not a skip to count.
    assert "no_own_plan 1" not in out
    assert "Failed: 1 (post_failed 1)" in out
    assert "Posted by hand: 1 (1 of a regenerated version)" in out


def test_an_unreadable_history_fails_loudly():
    result = _run(error=RuntimeError("db down"))
    assert result.exit_code == 1
    assert "Could not read" in result.output


def test_an_empty_history_says_so():
    result = _run()
    assert result.exit_code == 0
    assert "Nothing recorded" in result.output
