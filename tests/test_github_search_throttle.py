"""GitHub code-search throttling must be distinguishable from "no hits".

The bug being pinned: `search_relevant_files` treated every non-200 as an
empty result. GitHub caps code search at 10 requests/minute (plus a secondary
limit on bursts) and refuses with 403 — the same status a permissions failure
uses. One plan generation can spend that whole budget, so the code-grounding
critic routinely concluded "the repo doesn't implement this" when it had never
been allowed to look, and left the warning at WARN with nothing saying the
pass hadn't run. QA then chases the false positive the critic exists to remove.

The discrimination tests below are the heart of it: a rate-limit 403 and a
permissions 403 must not be handled the same way.
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.app.code_grounding_critic import mark_recheck_unavailable
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
# _throttle_retry_after — telling a throttle apart from everything else
# ---------------------------------------------------------------------------


def test_success_is_not_a_throttle():
    assert GitHubClient._throttle_retry_after(_response(200, json_body={})) is None


def test_not_found_is_not_a_throttle():
    assert GitHubClient._throttle_retry_after(_response(404, body="nope")) is None


def test_permissions_403_is_not_a_throttle():
    """The discrimination that matters. A 403 with no rate signals is a real
    authorization failure — retrying it wastes a minute and it must not be
    reported to the critic as "throttled, evidence unknown"."""
    r = _response(
        403,
        body='{"message": "Resource not accessible by personal access token"}',
    )
    assert GitHubClient._throttle_retry_after(r) is None


def test_retry_after_header_is_honoured():
    r = _response(403, headers={"retry-after": "7"}, body="slow down")
    assert GitHubClient._throttle_retry_after(r) == 7.0


def test_exhausted_primary_limit_uses_the_reset_window():
    import time

    reset = time.time() + 42
    r = _response(
        403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
        body="rate limit",
    )
    wait = GitHubClient._throttle_retry_after(r)
    assert wait is not None and 35 < wait <= 42


def test_exhausted_primary_limit_without_a_reset_falls_back_to_a_minute():
    r = _response(403, headers={"x-ratelimit-remaining": "0"}, body="")
    assert GitHubClient._throttle_retry_after(r) == 60.0


def test_secondary_rate_limit_is_detected_from_the_body():
    """The secondary limit ships no rate headers at all — only the message."""
    r = _response(
        403,
        body='{"message": "You have exceeded a secondary rate limit."}',
    )
    assert GitHubClient._throttle_retry_after(r) == 60.0


def test_429_is_treated_as_a_throttle():
    r = _response(429, headers={"retry-after": "3"}, body="")
    assert GitHubClient._throttle_retry_after(r) == 3.0


def test_malformed_retry_after_does_not_crash():
    """A junk header must fall through to the other signals, not raise."""
    r = _response(403, headers={"retry-after": "soon"}, body="rate limit")
    assert GitHubClient._throttle_retry_after(r) == 60.0


# ---------------------------------------------------------------------------
# search_relevant_files — backoff, propagation, and cache
# ---------------------------------------------------------------------------


class _FakeHttp:
    """Stands in for httpx.AsyncClient, returning queued responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.search_calls = 0
        self.urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.urls.append(url)
        if "/search/code" in url:
            self.search_calls += 1
        return self._responses.pop(0)


_HIT = {
    "items": [
        {
            "path": "packages/engine/src/calculators/buyerNetSheet.ts",
            "repository": {"default_branch": "main"},
        }
    ]
}
# base64 of "const x = 1"
_FILE = {"content": "Y29uc3QgeCA9IDE="}


def _run(fake, coro_factory):
    import asyncio

    with (
        patch("src.app.github_client.httpx.AsyncClient", return_value=fake),
        patch("src.app.github_client.asyncio.sleep", AsyncMock()),
    ):
        return asyncio.run(coro_factory())


def test_a_throttled_search_retries_once_and_succeeds():
    """The recovery path — the code-search window is a minute wide, so one
    wait usually gets the evidence rather than losing it."""
    fake = _FakeHttp([
        _response(403, headers={"x-ratelimit-remaining": "0"}, body="rate limit"),
        _response(200, json_body=_HIT),
        _response(200, json_body=_FILE),
    ])
    client = GitHubClient(token="t")
    results = _run(fake, lambda: client.search_relevant_files("o/r", "buyer net sheet"))

    assert fake.search_calls == 2
    assert [r["path"] for r in results] == [
        "packages/engine/src/calculators/buyerNetSheet.ts"
    ]


def test_a_persistent_throttle_raises_instead_of_returning_empty():
    """The core fix. Returning [] here is what made the critic report a
    false negative, so the throttle has to reach the caller."""
    fake = _FakeHttp([
        _response(403, headers={"x-ratelimit-remaining": "0"}, body="rate limit"),
        _response(403, headers={"x-ratelimit-remaining": "0"}, body="rate limit"),
    ])
    client = GitHubClient(token="t")

    with pytest.raises(GitHubSearchThrottled):
        _run(fake, lambda: client.search_relevant_files("o/r", "buyer net sheet"))
    assert fake.search_calls == 2


def test_a_permissions_403_still_degrades_to_no_hits():
    """Unchanged behaviour for the non-rate case: no retry, no raise. A repo
    we genuinely can't read shouldn't stall generation for 15 seconds."""
    fake = _FakeHttp([
        _response(403, body='{"message": "Resource not accessible"}'),
    ])
    client = GitHubClient(token="t")
    results = _run(fake, lambda: client.search_relevant_files("o/r", "q"))

    assert results == []
    assert fake.search_calls == 1


def test_repeated_queries_hit_the_cache_not_the_api():
    """Budget control: one generation can want more searches than the 10/min
    allowance, so an identical (repo, query) must not spend a second call."""
    fake = _FakeHttp([
        _response(200, json_body=_HIT),
        _response(200, json_body=_FILE),
    ])
    client = GitHubClient(token="t")

    async def _twice():
        first = await client.search_relevant_files("o/r", "same query")
        second = await client.search_relevant_files("o/r", "same query")
        return first, second

    first, second = _run(fake, _twice)
    assert first == second
    assert fake.search_calls == 1


def test_an_empty_result_is_cached_too():
    """"Searched and found nothing" is a real answer worth remembering — it's
    the fact the critic is entitled to act on."""
    fake = _FakeHttp([
        _response(200, json_body={"items": []}),
    ])
    client = GitHubClient(token="t")

    async def _twice():
        await client.search_relevant_files("o/r", "q")
        await client.search_relevant_files("o/r", "q")

    _run(fake, _twice)
    assert fake.search_calls == 1


# ---------------------------------------------------------------------------
# mark_recheck_unavailable — saying so, rather than staying silent
# ---------------------------------------------------------------------------


def _warning():
    return {
        "ac_id": "AC1",
        "missing_element": "Empty buffer does not overwrite cached audio",
        "explanation": "Critic pass: the AC says nothing about caching.",
        "source": "critic_ac",
        "severity": "warn",
    }


def test_marking_keeps_severity_at_warn():
    """Downgrading to info would assert a confirmation we never got; dropping
    the warning would hide a real gap. It stays a WARN that admits it's
    unverified."""
    w = _warning()
    mark_recheck_unavailable([w])
    assert w["severity"] == "warn"
    assert w["recheck_status"] == "unavailable"
    assert w["recheck_unavailable_reason"] == "github_search_rate_limited"


def test_marking_says_unverified_in_the_text_the_ui_already_renders():
    w = _warning()
    mark_recheck_unavailable([w])
    assert "Critic pass: the AC says nothing about caching." in w["explanation"]
    assert "rate-limited" in w["explanation"]
    assert "unverified rather than confirmed" in w["explanation"]


def test_marking_is_idempotent():
    """The critic can be re-run over a plan; the note must not stack."""
    w = _warning()
    mark_recheck_unavailable([w])
    once = w["explanation"]
    assert mark_recheck_unavailable([w]) == []
    assert w["explanation"] == once


def test_marking_ignores_non_dict_entries():
    assert mark_recheck_unavailable([None, "nope", 3]) == []


# ---------------------------------------------------------------------------
# The critic end-to-end: a throttle marks the warning and skips the LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_critic_marks_warnings_when_search_is_throttled():
    from src.app.config import settings
    from src.app.services import test_plan_generator

    class _Plan:
        def __init__(self, warning):
            self.grounding_warnings = [warning]
            self.edge_cases = [
                {
                    "title": warning["missing_element"],
                    "steps": ["do a thing"],
                    "expected": "it works",
                    "needs_manual_verification": True,
                }
            ]
            self.happy_path = []
            self.integration_tests = []
            self.regression_checklist = []

    class _LLM:
        def __init__(self):
            self.called = False

        async def verify_code_grounding(self, cases):
            self.called = True
            return {}

    warning = _warning()
    plan = _Plan(warning)
    llm = _LLM()
    dev_infos = [
        {
            "ticket_key": "SK-1",
            "development_info": {
                "pull_requests": [
                    {
                        "title": "Ship the thing",
                        "status": "merged",
                        # extract_repos reads `repository`, not the URL.
                        "repository": "acme/agent-calculator",
                        "url": "https://github.com/acme/agent-calculator/pull/1",
                    }
                ],
                "commits": [],
                "branches": [],
            },
        }
    ]

    async def _throttled(self, repo, query, max_files=3):
        raise GitHubSearchThrottled("rate-limited", retry_after=60)

    with (
        patch.object(settings, "github_token", "t"),
        patch.object(settings, "code_grounding_recheck_enabled", True),
        patch.object(GitHubClient, "search_relevant_files", _throttled),
    ):
        await test_plan_generator.run_code_grounding_critic(llm, plan, dev_infos)

    # No evidence was gathered, so there was nothing to ask the model about.
    assert llm.called is False
    # But the warning now admits the recheck didn't happen.
    assert warning["recheck_status"] == "unavailable"
    assert warning["severity"] == "warn"
    assert "rate-limited" in warning["explanation"]
    # And the case keeps its badge — nothing was confirmed either way.
    assert plan.edge_cases[0]["needs_manual_verification"] is True
