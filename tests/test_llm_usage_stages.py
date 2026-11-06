"""Per-step time and tokens on a plan run (2026-11-05).

Plan generation went from ~1 to ~8 minutes over Sep–Nov and a run recorded
only its total, so which step grew could not be read from the data. Each step
now records its wall-clock time and the Claude calls made inside it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.app.db.models.plan import PlanFormat
from src.app.services import llm_usage, run_tracker
from src.cli.commands import timings_cmd
from src.cli.main import app
from typer.testing import CliRunner

CALL = {"input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 10}


def test_calls_inside_a_step_count_toward_it_and_the_total():
    async def run():
        meter = llm_usage.begin()
        with llm_usage.stage("generate"):
            llm_usage.record("claude-opus-5", CALL)
            await asyncio.sleep(0.01)
        llm_usage.record("claude-opus-5", CALL)
        return meter

    meter = asyncio.run(run())
    steps = llm_usage.stage_summary(meter)
    assert meter["calls"] == 2
    assert steps["generate"]["calls"] == 1
    assert steps["generate"]["prompt_tokens"] == 150
    assert steps["generate"]["ms"] >= 10
    assert steps["generate"]["cost_usd"] > 0
    # A call no step claimed is shown, not dropped.
    assert steps[llm_usage.UNATTRIBUTED]["calls"] == 1


def test_concurrent_steps_keep_their_own_calls_and_time():
    async def critic(n):
        llm_usage.record("claude-opus-5", CALL)
        await asyncio.sleep(0.01 * n)
        llm_usage.record("claude-opus-5", CALL)

    async def run():
        meter = llm_usage.begin()
        await asyncio.gather(llm_usage.timed("a", critic(1)), llm_usage.timed("b", critic(3)))
        return meter

    steps = llm_usage.stage_summary(asyncio.run(run()))
    assert steps["a"]["calls"] == 2 and steps["b"]["calls"] == 2
    assert steps["b"]["ms"] > steps["a"]["ms"]
    assert llm_usage.UNATTRIBUTED not in steps


def test_a_step_entered_twice_accumulates():
    async def run():
        meter = llm_usage.begin()
        for _ in range(2):
            with llm_usage.stage("critic"):
                llm_usage.record("claude-opus-5", CALL)
        return meter

    assert llm_usage.stage_summary(asyncio.run(run()))["critic"]["calls"] == 2


def test_a_step_that_raises_still_records_its_time():
    async def boom():
        await asyncio.sleep(0.01)
        raise TimeoutError

    async def run():
        meter = llm_usage.begin()
        with pytest.raises(TimeoutError):
            await llm_usage.timed("generate", boom())
        return meter

    assert llm_usage.stage_summary(asyncio.run(run()))["generate"]["ms"] >= 10


def test_steps_without_a_meter_are_a_no_op():
    async def run():
        llm_usage._meter.set(None)
        with llm_usage.stage("x"):
            llm_usage.record("claude-opus-5", CALL)
        return llm_usage.stage_summary()

    assert asyncio.run(run()) is None


@pytest.mark.asyncio
async def test_a_saved_plan_run_records_its_steps():
    llm_usage.begin()
    with llm_usage.stage("generate"):
        llm_usage.record("claude-opus-5", CALL)
    mark_completed = AsyncMock()
    with patch.object(run_tracker, "get_db", return_value=object()), \
         patch.object(run_tracker.crud, "get_by_id", AsyncMock(return_value=SimpleNamespace(ticket_keys=["A", "B"]))), \
         patch.object(run_tracker.run_repository, "mark_completed", mark_completed), \
         patch.object(run_tracker.plan_repository, "save_with_cases",
                      AsyncMock(return_value=SimpleNamespace(id=1, version=1))):
        await run_tracker.complete_with_plan(
            run_tracker.RunContext(run_id=7), plan_body="{}", plan_format=PlanFormat.json, cases=[])
    assert mark_completed.await_args.kwargs["stages"]["generate"]["calls"] == 1


@pytest.mark.asyncio
async def test_a_failed_run_records_the_steps_it_was_given():
    mark_failed = AsyncMock()
    stages = {"generate": {"ms": 890_000, "calls": 1, "prompt_tokens": 1, "output_tokens": 0, "cost_usd": 0.0}}
    with patch.object(run_tracker, "get_db", return_value=object()), \
         patch.object(run_tracker.crud, "get_by_id", AsyncMock(return_value=SimpleNamespace())), \
         patch.object(run_tracker.run_repository, "mark_failed", mark_failed):
        await run_tracker.fail(run_tracker.RunContext(run_id=7), error_code="llm_timeout", stages=stages)
    assert mark_failed.await_args.kwargs["stages"] == stages


def test_timings_shows_steps_slowest_first_and_on_the_slowest_runs():
    from datetime import datetime, timezone
    at = datetime(2026, 11, 5, 18, tzinfo=timezone.utc)

    def step(ms):
        return {"ms": ms, "calls": 2, "prompt_tokens": 1000, "output_tokens": 10, "cost_usd": 0.5}

    runs = [
        {"ticket_keys": ["SK-1"], "latency_ms": 500_000, "status": "ok", "trigger": "watcher",
         "created_at": at, "stages": {"generate": step(120_000), "reachability": step(240_000)}},
        {"ticket_keys": ["SK-2"], "latency_ms": 60_000, "status": "ok", "trigger": "ui", "created_at": at},
    ]
    with patch.object(timings_cmd, "_load", AsyncMock(return_value=runs)):
        result = CliRunner().invoke(app, ["timings", "--days", "7"])
    out = result.output
    assert result.exit_code == 0, out
    assert "Per step · 1 run with step times" in out
    steps = out[out.index("Per step"):]
    assert steps.index("reachability") < steps.index("generate")
    assert "reachability 4:00" in out


def test_timings_says_when_no_run_has_steps():
    from datetime import datetime, timezone
    runs = [{"ticket_keys": ["SK-1"], "latency_ms": 60_000, "status": "ok", "trigger": "ui",
             "created_at": datetime(2026, 11, 1, tzinfo=timezone.utc)}]
    with patch.object(timings_cmd, "_load", AsyncMock(return_value=runs)):
        result = CliRunner().invoke(app, ["timings"])
    assert "No per-step times recorded" in result.output


def test_thinking_tokens_are_recorded_per_step_as_part_of_output():
    async def run():
        meter = llm_usage.begin()
        with llm_usage.stage("generate"):
            llm_usage.record("claude-opus-5", {**CALL, "output_tokens": 1000,
                                               "output_tokens_details": {"thinking_tokens": 700}})
        return meter

    meter = asyncio.run(run())
    step = llm_usage.stage_summary(meter)["generate"]
    assert step["output_tokens"] == 1000 and step["thinking_tokens"] == 700
    assert meter["thinking_tokens"] == 700


class _Stop(Exception):
    pass


@pytest.mark.parametrize("effort, expected", [(None, None), ("medium", {"effort": "medium"})])
@pytest.mark.asyncio
async def test_generate_effort_reaches_the_plan_call_only_when_set(monkeypatch, effort, expected):
    from src.app import llm_client as mod
    monkeypatch.setattr(mod.settings, "anthropic_api_key", "sk-test", raising=False)
    monkeypatch.setattr(mod.settings, "generate_effort", effort)
    client = mod.ClaudeClient()
    client.model = "claude-opus-5"
    sent = []

    async def capture(payload, **kw):
        sent.append(payload)
        raise _Stop

    monkeypatch.setattr(client, "_post_messages", capture)
    with pytest.raises(Exception):
        await client.generate_test_plan(ticket_key="SK-1", summary="s", description="d", testing_context={})
    assert sent, "the plan call was never made"
    assert sent[0].get("output_config") == expected
