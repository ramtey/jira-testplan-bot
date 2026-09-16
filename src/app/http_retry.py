"""An httpx client that treats a throttle as a pause rather than an answer.

`jira_client` opened a bare `httpx.AsyncClient` at 35 places and handled a
non-200 by returning None or an empty list. Every one of those is the repo's
recurring bug: a transient failure recorded as a fact about the ticket. A 429
while listing comments meant "this ticket has no comments"; a 502 on
dev-status meant "no implementation found". `ClaudeClient._post_messages`
already solved this for the Anthropic API — this is the same policy, packaged
so a call site gets it by construction instead of by remembering.

This is a transport rather than an `AsyncClient` subclass on purpose. The
suites patch `httpx.AsyncClient` globally, so a call site naming any other
class stops being interceptable and nineteen tests start making real network
calls — which is how the first attempt at this was caught. A transport keeps
every call site saying `httpx.AsyncClient(...)`, so a patch still bites and
each site keeps its own timeout, redirect policy and error handling.

**A long Retry-After is not something to sleep through.** Figma answered one
of these with 241725 seconds — 2.8 days — on 2026-09-16. Waiting is the wrong
response to a spent quota: the request should come back throttled so the
caller can say it could not look, which is a different sentence from "there is
nothing there". So a Retry-After longer than `_MAX_SLEEP_S` ends the retries
and hands the throttled response back, rather than blocking a plan generation
behind it.
"""

from __future__ import annotations

import asyncio
import logging
import random

import httpx

logger = logging.getLogger(__name__)

#: Statuses worth a second attempt. A 429 is a throttle, the 5xx family is the
#: server having a bad moment, 408 is the server saying it gave up waiting.
#: 401/403/404 are answers — retrying them just wastes the budget and delays a
#: real error.
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

_MAX_ATTEMPTS = 3
_BASE_DELAY_S = 0.5
#: Never sleep longer than this inside a request, whatever Retry-After says.
_MAX_SLEEP_S = 10.0


def retry_after_seconds(response: httpx.Response | None) -> float | None:
    """The server's own Retry-After in seconds, when it gave one."""
    if response is None:
        return None
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        # The HTTP-date form is legal and nobody here sends it. Falling back to
        # the backoff is better than parsing dates wrong.
        return None


def backoff_delay(attempt: int, response: httpx.Response | None) -> float | None:
    """How long to wait before the next attempt, or None to stop retrying.

    None means "the server asked for longer than we are willing to block" —
    the caller gets the throttled response and can report that it could not
    read, instead of a plan generation stalling behind someone else's quota.
    """
    asked = retry_after_seconds(response)
    if asked is not None:
        return asked if asked <= _MAX_SLEEP_S else None
    # Exponential with jitter: two plan generations that hit the same limit
    # should not march back in lockstep.
    return min(_BASE_DELAY_S * (2 ** attempt) + random.uniform(0, 0.25), _MAX_SLEEP_S)


class RetryingTransport(httpx.AsyncBaseTransport):
    """Wraps a transport, retrying throttles and transient server errors."""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        for attempt in range(_MAX_ATTEMPTS):
            is_last = attempt == _MAX_ATTEMPTS - 1
            try:
                response = await self._inner.handle_async_request(request)
            except (httpx.TimeoutException, httpx.TransportError):
                if is_last:
                    raise
                delay = backoff_delay(attempt, None)
                if delay is None:
                    raise
            else:
                if is_last or response.status_code not in RETRYABLE_STATUSES:
                    return response
                delay = backoff_delay(attempt, response)
                if delay is None:
                    logger.warning(
                        "%s %s throttled with Retry-After beyond what a request "
                        "may wait; returning the throttle so the caller can say "
                        "it could not read", request.method, request.url,
                    )
                    return response
                # The response body still holds its connection, and a retried
                # response is never read — release it rather than leak the slot.
                await response.aclose()
                logger.info(
                    "%s %s returned %s; retrying in %.1fs (attempt %d of %d)",
                    request.method, request.url, response.status_code,
                    delay, attempt + 1, _MAX_ATTEMPTS,
                )
            await asyncio.sleep(delay)
        raise RuntimeError("retry loop ended without a response")

    async def aclose(self) -> None:
        await self._inner.aclose()


def retrying_client(**kwargs) -> httpx.AsyncClient:
    """An `httpx.AsyncClient` that retries, built the patchable way.

    Constructed through `httpx.AsyncClient` by name so a suite patching that
    class still intercepts. A fresh transport per client, because closing the
    client closes its transport and a shared one would be dead afterwards.
    """
    kwargs.setdefault("transport", RetryingTransport())
    return httpx.AsyncClient(**kwargs)
