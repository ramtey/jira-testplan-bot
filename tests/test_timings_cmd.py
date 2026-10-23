"""`testplan timings`: how long generation takes, typical and slowest."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from src.cli.commands import timings_cmd
from src.cli.main import app

runner = CliRunner()
AT = datetime(2026, 10, 23, 18, tzinfo=timezone.utc)


def _run(runs=(), error=None, args=("--days", "7")):
    load = AsyncMock(return_value=list(runs), side_effect=error)
    with patch.object(timings_cmd, "_load", load):
        return runner.invoke(app, ["timings", *args])


def _r(key, ms, status="ok", trigger="watcher", **extra):
    return {"ticket_keys": [key], "latency_ms": ms, "status": status,
            "trigger": trigger, "created_at": AT, **extra}


def test_summarises_per_trigger_and_lists_the_slowest():
    runs = [
        _r("SK-1", 60_000), _r("SK-2", 180_000), _r("SK-3", 300_000),
        _r("SK-4", 120_000, trigger="ui"),
        _r("SK-5", 30_000, trigger=None),
        _r("SK-6", 600_000, status="error", error_code="llm_timeout"),
    ]
    result = _run(runs)
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Plans generated: 5" in out
    assert "watcher: 3 — median 3:00 · p90 5:00 · max 5:00" in out
    assert "ui: 1" in out
    assert "unknown: 1" in out
    assert "Failed: 1" in out
    # The failed run is the slowest and must appear first, with why it failed.
    assert out.index("SK-6") < out.index("SK-3")
    assert "llm_timeout" in out


def test_runs_with_no_time_are_counted_not_averaged_in():
    result = _run([_r("SK-1", 240_000), _r("SK-2", 0)])
    assert result.exit_code == 0, result.output
    assert "Plans generated: 1 — median 4:00" in result.output
    assert "No time recorded: 1" in result.output


def test_top_limits_the_slowest_list():
    runs = [_r(f"SK-{i}", i * 1000) for i in range(1, 6)]
    result = _run(runs, args=("--top", "2"))
    assert result.exit_code == 0, result.output
    assert "Slowest 2" in result.output
    assert "SK-5" in result.output and "SK-4" in result.output
    assert "SK-1 " not in result.output


def test_empty_window_says_so():
    result = _run([])
    assert result.exit_code == 0
    assert "No test plan runs recorded" in result.output


def test_an_unreadable_history_fails_loudly():
    result = _run(error=RuntimeError("Atlas down"))
    assert result.exit_code == 1
    assert "Could not read the run history" in result.output
