"""A throttle is a pause, not an answer.

`jira_client` opened a bare client at 35 places and read any non-200 as a fact
about the ticket: a 429 while listing comments meant "no comments", a 502 on
dev-status meant "no implementation found". That is the repo's recurring bug —
a failed lookup reported as an answer — and it had no retry and no 429
handling at any of those call sites.

The distinctions these tests pin:

  * retryable (throttle, server having a bad moment) vs. an answer (401, 403,
    404). Retrying an answer wastes the budget and delays a real error.
  * a short Retry-After, which is worth waiting out, vs. a long one, which is
    a spent quota. Figma answered one of these with 241725 seconds on
    2026-09-16; sleeping through that inside a request would hang a plan
    generation behind someone else's budget.
  * exhausted retries still hand back the throttle. Swallowing it into None
    would rebuild the exact bug this module exists to remove.
"""
import httpx
import pytest

from src.app import http_retry
from src.app.http_retry import RetryingTransport, backoff_delay, retrying_client


class FakeTransport(httpx.AsyncBaseTransport):
    """Returns queued responses (or raises queued exceptions), counting calls."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def handle_async_request(self, request):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _resp(status, headers=None):
    return httpx.Response(status, headers=headers or {}, content=b"{}")


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    slept = []

    async def _instant(seconds):
        slept.append(seconds)
    monkeypatch.setattr(http_retry.asyncio, "sleep", _instant)
    return slept


async def _send(transport, method="GET"):
    """Drive a real client whose transport is the retrying wrapper around the
    fake. Passing the fake as `transport=` to retrying_client would defeat the
    wrapper via setdefault, which is exactly how this helper was wrong first."""
    async with httpx.AsyncClient(transport=RetryingTransport(transport)) as client:
        return await client.request(method, "https://jira.example.com/rest/api/3/issue/SK-1")


class TestAThrottleIsRetried:
    @pytest.mark.asyncio
    async def test_a_429_is_retried_and_the_success_returned(self):
        t = FakeTransport(_resp(429), _resp(200))
        r = await _send(t)
        assert t.calls == 2
        assert r.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
    async def test_transient_server_errors_are_retried(self, status):
        t = FakeTransport(_resp(status), _resp(200))
        assert (await _send(t)).status_code == 200
        assert t.calls == 2

    @pytest.mark.asyncio
    async def test_a_timeout_is_retried(self):
        t = FakeTransport(httpx.ConnectTimeout("slow"), _resp(200))
        assert (await _send(t)).status_code == 200
        assert t.calls == 2


class TestAnAnswerIsNotRetried:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [200, 201, 400, 401, 403, 404])
    async def test_it_is_returned_on_the_first_attempt(self, status):
        t = FakeTransport(_resp(status))
        r = await _send(t)
        assert t.calls == 1, "retrying an answer delays the real error"
        assert r.status_code == status


class TestTheCallerStillLearnsItWasThrottled:
    """The bug this module removes is a throttle read as an absence. Exhausting
    the retries must not turn one into the other."""

    @pytest.mark.asyncio
    async def test_a_standing_throttle_is_handed_back_not_swallowed(self):
        t = FakeTransport(_resp(429))
        r = await _send(t)
        assert t.calls == 3, "it should have used its attempts"
        assert r.status_code == 429, "the caller must be able to tell a 429 from no data"

    @pytest.mark.asyncio
    async def test_a_standing_timeout_still_raises(self):
        t = FakeTransport(httpx.ConnectTimeout("slow"))
        with pytest.raises(httpx.TimeoutException):
            await _send(t)
        assert t.calls == 3


class TestRetryAfter:
    def test_a_short_one_is_obeyed_exactly(self):
        assert backoff_delay(0, _resp(429, {"retry-after": "2"})) == 2.0

    def test_a_long_one_stops_the_retries(self):
        """Figma's 241725s, 2026-09-16. A spent quota is not something to
        sleep through inside a request."""
        assert backoff_delay(0, _resp(429, {"retry-after": "241725"})) is None

    def test_an_unparseable_one_falls_back_to_backoff(self):
        d = backoff_delay(0, _resp(429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}))
        assert d is not None and 0 < d <= 10

    def test_backoff_grows_and_stays_capped(self):
        assert backoff_delay(0, None) < backoff_delay(5, None) <= 10.0

    @pytest.mark.asyncio
    async def test_a_long_retry_after_returns_immediately_without_sleeping(
        self, no_real_sleeping
    ):
        t = FakeTransport(_resp(429, {"retry-after": "241725"}))
        r = await _send(t)
        assert t.calls == 1
        assert r.status_code == 429
        assert no_real_sleeping == [], "nothing should have waited 2.8 days"

    @pytest.mark.asyncio
    async def test_a_short_retry_after_is_waited_out(self, no_real_sleeping):
        t = FakeTransport(_resp(429, {"retry-after": "2"}), _resp(200))
        assert (await _send(t)).status_code == 200
        assert no_real_sleeping == [2.0]


class TestItStaysPatchable:
    """The first version subclassed AsyncClient. The suites patch
    `httpx.AsyncClient` globally, so 19 tests silently started making real
    network calls. Building through the class by name is load-bearing."""

    def test_the_client_is_built_through_httpx_async_client(self, monkeypatch):
        seen = {}

        def _fake(**kwargs):
            seen.update(kwargs)
            return "patched-client"
        monkeypatch.setattr(httpx, "AsyncClient", _fake)
        assert retrying_client(timeout=20) == "patched-client"
        assert seen["timeout"] == 20
        assert isinstance(seen["transport"], RetryingTransport)

    def test_every_jira_call_site_uses_it(self):
        """Coverage, not behaviour: a new bare client would reintroduce the bug
        at that one call site without failing anything else."""
        source = open("src/app/jira_client.py").read()
        assert "httpx.AsyncClient(" not in source, (
            "a call site builds a bare client and so has no retry"
        )
        assert source.count("retrying_client(") == 35
