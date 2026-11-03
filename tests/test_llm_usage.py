"""Plan runs record what their Claude calls cost, and two smaller recording
fixes raised in review (2026-11-03):

- ``runs`` showed 0 tokens and $0 for every plan: nothing passed usage to
  ``complete_with_plan``, which defaulted to zero.
- ``had_figma`` was 0 on every run: it checked ``testing_context`` while
  fetched Figma designs live under ``development_info.figma_context``.
- Bug Lens blames the default branch, so after the fix merges a line the fix
  touched blames to the fix itself and was shown as "Introduced by".
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.app import bug_lens_routes
from src.app.db.models.plan import PlanFormat
from src.app.models import GenerateTestPlanRequest
from src.app.services import llm_usage, run_tracker
from src.app.services.test_plan_generator import derive_context_flags


def test_cost_counts_cache_and_output_at_their_own_rates():
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
    }
    # Opus 5: $5 in, $25 out, writes 1.25x in, reads 0.1x in.
    assert llm_usage.cost_of("claude-opus-5", usage) == Decimal("5") + Decimal("25") + Decimal("6.25") + Decimal("0.5")


def test_longest_prefix_wins_so_opus_5_5_is_not_priced_as_opus_5():
    assert llm_usage.price_for("claude-opus-5-5") == (Decimal("4"), Decimal("20"))
    assert llm_usage.price_for("claude-opus-5") == (Decimal("5"), Decimal("25"))


def test_fast_mode_doubles_the_price():
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert llm_usage.cost_of("claude-opus-5", usage, fast=True) == Decimal("10")


def test_calls_outside_a_generation_are_not_counted():
    async def outside():
        llm_usage.record("claude-opus-5", {"input_tokens": 10, "output_tokens": 10})
        return llm_usage.current()

    assert asyncio.run(outside()) is None


def test_an_unpriced_model_keeps_its_tokens_and_says_its_cost_is_missing():
    async def run():
        meter = llm_usage.begin()
        llm_usage.record("some-future-model", {"input_tokens": 7, "output_tokens": 3})
        return meter

    meter = asyncio.run(run())
    assert meter["input_tokens"] == 7 and meter["output_tokens"] == 3
    assert meter["cost_usd"] == 0
    assert meter["unpriced_models"] == ["some-future-model"]


def test_parallel_calls_in_one_generation_share_the_meter():
    """Critics run under asyncio.gather; each task copies the context, but the
    meter dict itself is shared, so their usage still adds up."""

    async def one_call():
        llm_usage.record("claude-opus-5", {"input_tokens": 100, "output_tokens": 50})

    async def run():
        meter = llm_usage.begin()
        await asyncio.gather(one_call(), one_call(), one_call())
        return meter

    meter = asyncio.run(run())
    assert meter["calls"] == 3
    assert meter["input_tokens"] == 300
    assert meter["output_tokens"] == 150


def test_begin_starts_fresh_so_the_watcher_does_not_carry_one_ticket_into_the_next():
    async def run():
        llm_usage.begin()
        llm_usage.record("claude-opus-5", {"input_tokens": 100, "output_tokens": 50})
        return llm_usage.begin()

    assert asyncio.run(run())["input_tokens"] == 0


@pytest.mark.asyncio
async def test_a_saved_plan_run_records_the_meters_tokens_and_cost():
    llm_usage.begin()
    llm_usage.record(
        "claude-opus-5",
        {"input_tokens": 1000, "cache_read_input_tokens": 500, "output_tokens": 200},
    )
    mark_completed = AsyncMock()
    run = SimpleNamespace(ticket_keys=["SK-1", "SK-2"])
    with patch.object(run_tracker, "get_db", return_value=object()), \
         patch.object(run_tracker.crud, "get_by_id", AsyncMock(return_value=run)), \
         patch.object(run_tracker.run_repository, "mark_completed", mark_completed), \
         patch.object(
             run_tracker.plan_repository,
             "save_with_cases",
             AsyncMock(return_value=SimpleNamespace(id=1, version=1)),
         ):
        await run_tracker.complete_with_plan(
            run_tracker.RunContext(run_id=42),
            plan_body="{}",
            plan_format=PlanFormat.json,
            cases=[],
        )

    kwargs = mark_completed.await_args.kwargs
    assert kwargs["prompt_tokens"] == 1500
    assert kwargs["output_tokens"] == 200
    assert kwargs["cost_usd"] > 0


def _request(**kwargs) -> GenerateTestPlanRequest:
    return GenerateTestPlanRequest(ticket_key="SK-1", summary="s", issue_type="Story", **kwargs)


def test_fetched_figma_designs_set_had_figma():
    req = _request(development_info={"figma_context": {"file_name": "Checkout", "file_key": "abc"}})
    assert derive_context_flags(req)["had_figma"] is True


def test_no_figma_anywhere_leaves_had_figma_false():
    req = _request(development_info={"figma_context": None})
    assert derive_context_flags(req)["had_figma"] is False


@pytest.mark.asyncio
async def test_blame_on_the_tickets_own_fix_is_not_shown_as_the_cause():
    blame = {"sha": "abc1234", "short": "abc1234", "pr_number": 812, "pr_url": "https://github.com/acme/web/pull/812"}
    client = SimpleNamespace(blame_line=AsyncMock(return_value=blame))
    own = bug_lens_routes._pr_refs(
        [{"pull_requests": [{"url": "https://github.com/Acme/web/pull/812"}]}]
    )
    with patch.object(bug_lens_routes, "GitHubClient", return_value=client), \
         patch.object(bug_lens_routes.settings, "github_token", "t"):
        result = await bug_lens_routes._compute_blame_evidence(
            [{"path": "src/a.ts", "line": 3}], {"src/a.ts": "acme/web"}, own
        )
    assert result[0]["blamed_own_fix"] is True
    assert "own PR" in result[0]["notes"]


@pytest.mark.asyncio
async def test_blame_on_an_unrelated_pr_is_left_as_the_introducing_change():
    blame = {"sha": "abc1234", "short": "abc1234", "pr_number": 5}
    client = SimpleNamespace(blame_line=AsyncMock(return_value=blame))
    own = {("acme/web", 812)}
    with patch.object(bug_lens_routes, "GitHubClient", return_value=client), \
         patch.object(bug_lens_routes.settings, "github_token", "t"):
        result = await bug_lens_routes._compute_blame_evidence(
            [{"path": "src/a.ts", "line": 3}], {"src/a.ts": "acme/web"}, own
        )
    assert "blamed_own_fix" not in result[0]
