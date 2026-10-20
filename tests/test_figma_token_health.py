"""The Figma health check must probe what the app actually reads.

Pinned on 2026-09-16, when the check said Figma was healthy and design
context had been missing from every plan for days. /v1/me answered its
usual 403 "invalid scope" — which this repo already, correctly, reads as
"the token is live" — while every /v1/files call came back 429 with a
Retry-After of 241725 seconds. Both facts were true. Only one of them was
about the thing the bot does.

The rule these tests encode: a check that is only allowed to prove the
token authenticates must SAY that it did not check the quota, rather than
report a clean bill of health.
"""
import httpx
import pytest

from src.app.token_service import TokenErrorType, TokenHealthService


def _resp(status, *, body="", json_body=None, headers=None, url="https://api.figma.com/v1/files/x"):
    request = httpx.Request("GET", url)
    if json_body is not None:
        return httpx.Response(status, json=json_body, headers=headers or {}, request=request)
    return httpx.Response(status, text=body, headers=headers or {}, request=request)


@pytest.fixture
def figma(monkeypatch):
    """A configured token, and a recorder for the URL the check requests."""
    from src.app import token_service as ts
    monkeypatch.setattr(ts.settings, "figma_token", "fig-token", raising=False)
    monkeypatch.setattr(ts.settings, "figma_healthcheck_file_key", None, raising=False)
    seen = {}

    def install(response):
        async def _get(self, url, **kwargs):
            seen["url"] = url
            seen["headers"] = kwargs.get("headers")
            return response
        monkeypatch.setattr(httpx.AsyncClient, "get", _get)
    return install, seen, ts


class TestItProbesTheEndpointTheAppUses:
    @pytest.mark.asyncio
    async def test_it_reads_files_not_me(self, figma):
        install, seen, _ = figma
        install(_resp(404, json_body={"status": 404, "err": "Not found"}))
        await TokenHealthService().validate_figma_token()
        assert "/v1/files/" in seen["url"]
        assert "/v1/me" not in seen["url"]

    @pytest.mark.asyncio
    async def test_a_configured_key_is_the_one_read(self, figma, monkeypatch):
        install, seen, ts = figma
        monkeypatch.setattr(ts.settings, "figma_healthcheck_file_key", "REALKEY123", raising=False)
        install(_resp(200, json_body={"name": "Buyer Home Estimate"}))
        status = await TokenHealthService().validate_figma_token()
        assert seen["url"].endswith("/v1/files/REALKEY123")
        assert status.is_valid
        assert status.details["file_name"] == "Buyer Home Estimate"


class TestItSaysWhatItCouldNotCheck:
    """Figma bills /v1/files by what it returns, so a nonexistent key answers
    404 for free — and keeps answering 404 while the real-file quota is spent.
    A pass on that probe is not evidence design context works."""

    @pytest.mark.asyncio
    async def test_unconfigured_probe_admits_it_skipped_the_quota(self, figma):
        install, _, _ = figma
        install(_resp(404, json_body={"status": 404, "err": "Not found"}))
        status = await TokenHealthService().validate_figma_token()
        assert status.is_valid
        assert status.details["quota_checked"] is False
        assert "FIGMA_HEALTHCHECK_FILE_KEY" in status.error_message

    @pytest.mark.asyncio
    async def test_a_missing_configured_file_is_a_blind_check_not_a_pass(
        self, figma, monkeypatch
    ):
        install, _, ts = figma
        monkeypatch.setattr(ts.settings, "figma_healthcheck_file_key", "GONE", raising=False)
        install(_resp(404, json_body={"status": 404, "err": "Not found"}))
        status = await TokenHealthService().validate_figma_token()
        assert not status.is_valid
        assert status.error_type == TokenErrorType.INSUFFICIENT_PERMISSIONS


class TestAQuotaOutageIsNotSilent:
    """The condition that motivated this file: is_valid=True on a 429 meant
    the watcher never notified, because `if status.is_valid: return False`
    is how queue_watcher decides something is a problem."""

    @pytest.mark.asyncio
    async def test_a_multi_day_quota_outage_is_reported(self, figma):
        install, _, _ = figma
        install(_resp(429, json_body={"status": 429, "err": "Rate limit exceeded"},
                      headers={"retry-after": "241725"}))
        status = await TokenHealthService().validate_figma_token()
        assert not status.is_valid, "a 2.8-day outage must reach the watcher"
        assert status.error_type == TokenErrorType.RATE_LIMITED
        assert status.details["retry_after_seconds"] == 241725
        assert "2.8 days" in status.error_message
        # The token needs no action; the capability does. Both must be said.
        assert "token itself is fine" in status.error_message
        assert "design context is missing" in status.error_message

    @pytest.mark.asyncio
    async def test_a_burst_throttle_does_not_wake_anyone(self, figma):
        install, _, _ = figma
        install(_resp(429, json_body={"status": 429, "err": "Rate limit exceeded"},
                      headers={"retry-after": "30"}))
        status = await TokenHealthService().validate_figma_token()
        assert status.is_valid, "a 30s throttle clears itself"
        assert status.error_type == TokenErrorType.RATE_LIMITED

    @pytest.mark.asyncio
    async def test_a_429_without_retry_after_is_treated_as_serious(self, figma):
        """Unknown duration is not evidence of a short one."""
        install, _, _ = figma
        install(_resp(429, json_body={"status": 429, "err": "Rate limit exceeded"}))
        status = await TokenHealthService().validate_figma_token()
        assert not status.is_valid


class TestARejectedTokenIsStillARejectedToken:
    @pytest.mark.asyncio
    async def test_an_outright_rejection_is_expired(self, figma):
        install, _, _ = figma
        install(_resp(403, json_body={"status": 403, "err": "Invalid token"}))
        status = await TokenHealthService().validate_figma_token()
        assert not status.is_valid
        assert status.error_type == TokenErrorType.EXPIRED

    @pytest.mark.asyncio
    async def test_a_scope_complaint_still_means_the_token_is_live(self, figma):
        """Kept from the /v1/me era: a scope error proves it authenticated."""
        install, _, _ = figma
        install(_resp(403, json_body={"error": True, "message": 'Invalid scope: ["file_comments:read"]'}))
        status = await TokenHealthService().validate_figma_token()
        assert status.is_valid
        assert status.error_type == TokenErrorType.VALID


class TestTheProbeDoesNotSpendTheQuotaItChecks:
    """Pinned 2026-10-20. The probe reads a real file, and the header pill
    asked for it every five minutes per open tab. The watcher log showed the
    file-read quota refill and be spent again on a ~4-day cycle for a month,
    with not one "working again" in between. Every caller now shares one
    answer, and nothing probes while Figma's Retry-After is still running."""

    @pytest.fixture
    def counting(self, figma, monkeypatch):
        install, seen, ts = figma
        monkeypatch.setattr(ts.settings, "figma_healthcheck_file_key", "REALKEY123", raising=False)
        monkeypatch.setattr(ts.settings, "figma_probe_min_hours", 6.0, raising=False)
        calls = {"n": 0}

        def respond(response):
            async def _get(self, url, **kwargs):
                calls["n"] += 1
                return response
            monkeypatch.setattr(httpx.AsyncClient, "get", _get)
        return respond, calls, ts

    @pytest.mark.asyncio
    async def test_repeat_checks_inside_the_interval_reuse_one_probe(self, counting):
        respond, calls, _ = counting
        respond(_resp(200, json_body={"name": "Buyer Home Estimate"}))
        for _ in range(5):
            status = await TokenHealthService().validate_figma_token()
        assert calls["n"] == 1
        assert status.is_valid
        assert status.details["cached"] is True

    @pytest.mark.asyncio
    async def test_a_429_is_not_reprobed_before_its_retry_after(self, counting, monkeypatch):
        respond, calls, ts = counting
        respond(_resp(429, headers={"retry-after": str(3 * 86400)}))
        first = await TokenHealthService().validate_figma_token()
        assert first.error_type == TokenErrorType.RATE_LIMITED

        # Past the normal interval, still inside Figma's penalty.
        real_time = ts.time.time
        monkeypatch.setattr(ts.time, "time", lambda: real_time() + 2 * 86400)
        again = await TokenHealthService().validate_figma_token()
        assert calls["n"] == 1
        assert again.error_type == TokenErrorType.RATE_LIMITED
        assert not again.is_valid
        # The countdown is from now, not frozen at the probe's number.
        assert "expected in 24.0 hours" in again.error_message

    @pytest.mark.asyncio
    async def test_it_probes_again_once_the_answer_is_stale(self, counting, monkeypatch):
        respond, calls, ts = counting
        respond(_resp(200, json_body={"name": "f"}))
        await TokenHealthService().validate_figma_token()
        real_time = ts.time.time
        monkeypatch.setattr(ts.time, "time", lambda: real_time() + 7 * 3600)
        await TokenHealthService().validate_figma_token()
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_a_new_token_is_a_new_question(self, counting, monkeypatch):
        respond, calls, ts = counting
        respond(_resp(429, headers={"retry-after": "300000"}))
        await TokenHealthService().validate_figma_token()
        monkeypatch.setattr(ts.settings, "figma_token", "fresh-token", raising=False)
        respond(_resp(200, json_body={"name": "f"}))
        status = await TokenHealthService().validate_figma_token()
        assert calls["n"] == 2  # the stale 429 was not reused
        assert status.is_valid

    @pytest.mark.asyncio
    async def test_a_network_failure_is_not_remembered(self, counting, monkeypatch):
        respond, calls, _ = counting

        async def _boom(self, url, **kwargs):
            calls["n"] += 1
            raise httpx.ConnectError("offline")
        monkeypatch.setattr(httpx.AsyncClient, "get", _boom)
        await TokenHealthService().validate_figma_token()
        respond(_resp(200, json_body={"name": "f"}))
        status = await TokenHealthService().validate_figma_token()
        assert status.is_valid
        assert "cached" not in (status.details or {})
