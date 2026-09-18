"""Code search has to be paced, and a throttle has to stop the whole pass.

Companion to `test_github_search_throttle.py`, which pins how a throttle is
*reported*. This file pins the part that decides how often we get throttled at
all. Three findings from 2026-09-18, when three consecutive tickets all came
back with "regression-grounding critic ran partially":

1. GitHub allows 10 code searches per minute. A plan issues one per
   recheckable grounding warning plus one per control-naming checklist line,
   unpaced, so any ordinary ticket hit the wall by construction — and the
   regression critic, which runs last, was reliably the pass left starved.
2. The single retry waited `min(reset, 15s)` and then retried inside the same
   60s window, so when the reset was genuinely far out the wait bought nothing.
3. The code-grounding critic's throttle handling broke only the inner repo
   loop, so the pass kept searching after GitHub had refused it — despite a
   comment saying it stopped.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.app import github_client as gh
from src.app.github_client import GitHubClient, GitHubSearchThrottled


def _response(status: int, *, headers=None, body: str = "", json_body=None):
    request = httpx.Request("GET", "https://api.github.com/search/code")
    if json_body is not None:
        return httpx.Response(
            status_code=status, headers=headers or {}, json=json_body, request=request
        )
    return httpx.Response(
        status_code=status, headers=headers or {}, text=body, request=request
    )


# ---------------------------------------------------------------------------
# The limiter itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_searches_are_spaced_out_even_with_budget_to_spare():
    """GitHub's secondary limits police burstiness separately from the
    published quota: ten searches in six seconds gets refused with budget
    still showing. Spacing is not the same rule as the quota, so it applies
    even on the first few calls."""
    limiter = gh._CodeSearchRateLimiter(limit=10, window=60.0)
    with patch.object(gh, "_MIN_SEARCH_INTERVAL_SECONDS", 0.1):
        started = time.monotonic()
        for _ in range(3):
            await limiter.acquire()
        elapsed = time.monotonic() - started

    # Two gaps between three calls; the first is free.
    assert elapsed >= 0.2
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_githubs_own_headers_outrank_the_local_estimate():
    """The quota is a fixed server-side window — `x-ratelimit-reset` is an
    absolute epoch — so a locally-guessed sliding window drifts out of phase
    with it. When GitHub says the budget is gone, that is the fact."""
    limiter = gh._CodeSearchRateLimiter(limit=10, window=60.0)
    limiter.note_response(
        _response(
            200,
            headers={
                "x-ratelimit-resource": "code_search",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(time.time()) + 300),
            },
            json_body={"items": []},
        )
    )

    # Local accounting alone would happily allow this — nothing has been spent.
    with pytest.raises(GitHubSearchThrottled):
        await limiter.acquire()


@pytest.mark.asyncio
async def test_core_budget_headers_do_not_overwrite_the_search_budget():
    """The same client spends the 5000/hour `core` budget fetching file
    contents. Letting those headers land here would report a code-search
    budget that is never near zero."""
    limiter = gh._CodeSearchRateLimiter(limit=10, window=60.0)
    limiter.note_response(
        _response(
            200,
            headers={
                "x-ratelimit-resource": "core",
                "x-ratelimit-remaining": "4998",
                "x-ratelimit-reset": str(int(time.time()) + 3600),
            },
            json_body={},
        )
    )
    assert limiter._remaining is None


@pytest.mark.asyncio
async def test_a_short_wait_is_slept_through_rather_than_refused():
    """Pacing beats degrading when the next slot is close: the point is to get
    the evidence, not to give up tidily."""
    limiter = gh._CodeSearchRateLimiter(limit=2, window=0.3)
    await limiter.acquire()
    await limiter.acquire()

    started = time.monotonic()
    await limiter.acquire()  # must wait for the window to roll
    assert time.monotonic() - started >= 0.2


@pytest.mark.asyncio
async def test_a_long_wait_degrades_instead_of_stalling_the_plan():
    """A generation must not sit for the better part of a minute per search.
    Past the cap the limiter raises the same exception GitHub's 403 produces,
    so callers need no second code path."""
    limiter = gh._CodeSearchRateLimiter(limit=1, window=600.0)
    await limiter.acquire()

    with pytest.raises(GitHubSearchThrottled) as excinfo:
        await limiter.acquire()
    assert excinfo.value.retry_after > gh._MAX_SEARCH_WAIT_SECONDS


@pytest.mark.asyncio
async def test_github_refusing_us_spends_the_local_budget_too():
    """Otherwise every concurrent caller has to discover the throttle for
    itself, each one spending a request to be told the same thing."""
    limiter = gh._CodeSearchRateLimiter(limit=5, window=60.0)
    limiter.note_external_throttle(retry_after=300.0)

    with pytest.raises(GitHubSearchThrottled):
        await limiter.acquire()


@pytest.mark.asyncio
async def test_the_budget_is_shared_across_client_instances():
    """The regression critic builds its own GitHubClient after the
    code-grounding critic has already spent the quota. GitHub bills the token,
    so the budget cannot live on the object."""
    gh.reset_code_search_limiter()
    calls = []

    async def _fake_acquire():
        calls.append(1)

    with patch.object(gh._code_search_limiter, "acquire", _fake_acquire):
        assert GitHubClient(token="t") is not GitHubClient(token="t")
        await gh._code_search_limiter.acquire()
        await gh._code_search_limiter.acquire()

    assert len(calls) == 2


# ---------------------------------------------------------------------------
# The retry arithmetic
# ---------------------------------------------------------------------------


def _run(fake, coro_factory):
    with patch("httpx.AsyncClient", return_value=fake):
        return asyncio.run(coro_factory())


class _FakeHttp:
    def __init__(self, responses):
        self._responses = list(responses)
        self.search_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, url, headers=None):
        if "/search/code" in url:
            self.search_calls += 1
        return self._responses.pop(0)


def test_a_measured_reset_past_the_cap_skips_the_pointless_retry():
    """GitHub said the window rolls over in 300s. Sleeping the capped 15s and
    retrying can only fail, so the old code spent 15s per warning to learn
    nothing. One request, then an honest throttle."""
    gh.reset_code_search_limiter()
    fake = _FakeHttp([_response(429, headers={"retry-after": "300"}, body="rate limit")])
    client = GitHubClient(token="t")

    with pytest.raises(GitHubSearchThrottled) as excinfo:
        _run(fake, lambda: client.search_relevant_files("o/r", "buyer net sheet"))

    assert fake.search_calls == 1
    assert excinfo.value.retry_after == 300.0


def test_a_guessed_reset_still_retries():
    """A secondary rate limit names no duration, so `_throttle_retry_after`
    guesses 60s. A guess must not cancel a retry that often succeeds — these
    clear in seconds."""
    gh.reset_code_search_limiter()
    fake = _FakeHttp([
        _response(403, body="You have exceeded a secondary rate limit"),
        _response(200, json_body={"items": []}),
    ])
    client = GitHubClient(token="t")

    with patch("asyncio.sleep", new=AsyncMock()):
        results = _run(fake, lambda: client.search_relevant_files("o/r", "q"))

    assert fake.search_calls == 2
    assert results == []


def test_a_measured_reset_inside_the_cap_still_retries():
    gh.reset_code_search_limiter()
    fake = _FakeHttp([
        _response(429, headers={"retry-after": "2"}, body="rate limit"),
        _response(200, json_body={"items": []}),
    ])
    client = GitHubClient(token="t")

    with patch("asyncio.sleep", new=AsyncMock()):
        results = _run(fake, lambda: client.search_relevant_files("o/r", "q"))

    assert fake.search_calls == 2
    assert results == []


# ---------------------------------------------------------------------------
# The critic stops asking once GitHub has refused it
# ---------------------------------------------------------------------------


def _warning(element: str, ac_id: str):
    return {
        "ac_id": ac_id,
        "source": "critic_ac",
        "severity": "warn",
        "missing_element": element,
        "explanation": f"No code found for {element}",
    }


class _Plan:
    def __init__(self, warnings):
        self.grounding_warnings = list(warnings)
        self.edge_cases = [
            {
                "title": w["missing_element"],
                "steps": ["do a thing"],
                "expected": "it works",
                "needs_manual_verification": True,
            }
            for w in warnings
        ]
        self.happy_path = []
        self.integration_tests = []
        self.regression_checklist = []


class _LLM:
    async def verify_code_grounding(self, cases):
        return {}


_DEV_INFOS = [
    {
        "ticket_key": "SK-1",
        "development_info": {
            "pull_requests": [
                {
                    "title": "Ship the thing",
                    "status": "merged",
                    "repository": "acme/agent-calculator",
                    "url": "https://github.com/acme/agent-calculator/pull/1",
                }
            ],
            "commits": [],
            "branches": [],
        },
    }
]


@pytest.mark.asyncio
async def test_a_throttle_stops_the_pass_instead_of_asking_again():
    """The bug: `throttled` was re-initialised per warning and only ever broke
    the *repo* loop, so a refused pass kept issuing searches — two requests and
    a backoff sleep each — holding the window open while it was refused again.
    Every remaining warning must be marked without a further search."""
    from src.app.config import settings
    from src.app.services import test_plan_generator

    warnings = [_warning(f"Buyer net sheet row {i}", f"AC{i}") for i in range(5)]
    plan = _Plan(warnings)
    searches = []

    async def _throttled(self, repo, query, max_files=3):
        searches.append(query)
        raise GitHubSearchThrottled("rate-limited", retry_after=60)

    with (
        patch.object(settings, "github_token", "t"),
        patch.object(settings, "code_grounding_recheck_enabled", True),
        patch.object(GitHubClient, "search_relevant_files", _throttled),
    ):
        await test_plan_generator.run_code_grounding_critic(_LLM(), plan, _DEV_INFOS)

    # One search, not five.
    assert len(searches) == 1
    # And all five warnings still say the recheck did not happen — stopping
    # early must not be mistaken for the rest having been checked.
    assert [w.get("recheck_status") for w in warnings] == ["unavailable"] * 5
    assert all(w["severity"] == "warn" for w in warnings)
