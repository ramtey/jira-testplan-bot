"""
Test LLM client integration.

Run this to verify your LLM setup is working correctly.
"""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from unittest.mock import AsyncMock

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.app.llm_client import ClaudeClient, get_llm_client, LLMError, _is_observability_ticket


class TestObservabilityDetector:
    """Pure unit tests for the observability-ticket detector."""

    def test_alerting_summary_hits(self):
        assert _is_observability_ticket(
            "Add production alerting for elevated generative-AI error rates", ""
        )

    def test_grafana_loki_hit(self):
        assert _is_observability_ticket("", "Wire a new Grafana alert against Loki logs")

    def test_logql_hit(self):
        assert _is_observability_ticket("Add LogQL panel", None)

    def test_pagerduty_hit(self):
        assert _is_observability_ticket("Route critical alerts to PagerDuty", None)

    def test_failure_rate_hit(self):
        assert _is_observability_ticket("Alert when failure rate exceeds 5%", None)

    def test_invoice_does_not_falsely_trigger_on_alert_substring(self):
        # 'alerted' is not in the keyword list, and 'alert' uses \b boundaries —
        # incidental phrases shouldn't fire the guidance block.
        assert not _is_observability_ticket("Notify the user when payment fails", "")

    def test_plain_feature_ticket_is_not_observability(self):
        assert not _is_observability_ticket(
            "Add a Save button to the listing detail page",
            "Tapping Save persists the listing to the user's saved list.",
        )

    def test_login_does_not_match_log_keyword(self):
        # 'log in' / 'login' must not trigger the 'log ...' keywords —
        # they only match 'structured log', 'log line', 'log event', 'log spike'.
        assert not _is_observability_ticket("Fix login redirect loop", "Users can't log in")

    def test_none_inputs(self):
        assert not _is_observability_ticket(None, None)


@pytest.mark.asyncio
@pytest.mark.skip(reason="Manual integration test — requires a running LLM provider. Run directly: python tests/test_llm.py")
async def test_llm_generation():
    """Test generating a test plan with mock data."""
    print("=" * 80)
    print("TESTING LLM TEST PLAN GENERATION")
    print("=" * 80)
    print()

    # Mock ticket data
    ticket_key = "TEST-123"
    summary = "Add password reset functionality"
    description = """Users should be able to reset their password via email.

Acceptance Criteria:
- Given a user clicks 'Forgot Password', when they enter their email, then they receive a reset link
- Given a user clicks the reset link, when they enter a new password, then their password is updated
- Given the reset link is older than 24 hours, when they click it, then they see an expired message
"""
    testing_context = {
        "testDataNotes": "Test with valid and invalid email addresses",
        "rolesPermissions": "Any authenticated user",
        "riskAreas": "Email delivery, security token generation",
    }

    try:
        llm = get_llm_client()
        print(f"✓ Using LLM client: {llm.__class__.__name__}")
        print(f"✓ Generating test plan for ticket: {ticket_key}")
        print(f"✓ Summary: {summary}")
        print()
        print("⏳ Calling LLM... (this may take 30-120 seconds)")
        print()

        test_plan = await llm.generate_test_plan(
            ticket_key=ticket_key,
            summary=summary,
            description=description,
            testing_context=testing_context,
        )

        print("=" * 80)
        print("✅ TEST PLAN GENERATED SUCCESSFULLY!")
        print("=" * 80)
        print()

        print(f"📝 Happy Path Test Cases: {len(test_plan.happy_path)}")
        for i, test in enumerate(test_plan.happy_path, 1):
            print(f"\n  {i}. {test.get('title', 'Untitled')}")
            print(f"     Steps: {len(test.get('steps', []))}")
            print(f"     Expected: {test.get('expected', 'N/A')[:60]}...")

        print(f"\n🔍 Edge Cases: {len(test_plan.edge_cases)}")
        for i, test in enumerate(test_plan.edge_cases, 1):
            print(f"  {i}. {test.get('title', 'Untitled')}")

        print(f"\n🔄 Regression Checklist: {len(test_plan.regression_checklist)} items")
        for item in test_plan.regression_checklist[:3]:
            print(f"  - {item}")

        print()
        print("=" * 80)
        print("✅ LLM integration test passed!")
        print("=" * 80)
        return True

    except LLMError as e:
        print()
        print("=" * 80)
        print("❌ LLM ERROR")
        print("=" * 80)
        print(f"\nError: {e}\n")

        # Provide helpful troubleshooting
        if "ANTHROPIC_API_KEY not set" in str(e):
            print("💡 Troubleshooting:")
            print("   1. Get Claude API key from https://console.anthropic.com/")
            print("   2. Add to .env: ANTHROPIC_API_KEY=sk-ant-api03-...")
            print("   3. Or use local Ollama: LLM_PROVIDER=ollama")
        elif "Failed to connect to Ollama" in str(e):
            print("💡 Troubleshooting:")
            print("   1. Install Ollama: https://ollama.com/download")
            print("   2. Start Ollama: ollama serve")
            print("   3. Pull a model: ollama pull llama3.1")
            print("   4. Or use Claude API in .env: LLM_PROVIDER=claude")

        print()
        return False
    except Exception as e:
        print()
        print("=" * 80)
        print("❌ UNEXPECTED ERROR")
        print("=" * 80)
        print(f"\nError: {e}\n")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(test_llm_generation())
    sys.exit(0 if success else 1)


class TestFirstText:
    """Pulling the answer out of a response that may lead with thinking."""

    def test_plain_text_response(self):
        result = {"content": [{"type": "text", "text": "  hello  "}]}
        assert ClaudeClient._first_text(result) == "hello"

    def test_skips_leading_thinking_block(self):
        # Thinking-by-default models put this first, with empty text unless
        # display is opted in. content[0]["text"] used to raise KeyError here.
        result = {
            "content": [
                {"type": "thinking", "thinking": ""},
                {"type": "text", "text": "the answer"},
            ]
        }
        assert ClaudeClient._first_text(result) == "the answer"

    def test_no_text_block_raises_a_clear_error(self):
        result = {"content": [{"type": "thinking", "thinking": ""}]}
        try:
            ClaudeClient._first_text(result)
        except LLMError as e:
            assert "no text block" in str(e)
        else:
            raise AssertionError("expected LLMError")


class TestOverloadRetry:
    """529 "Overloaded" is transient — it must be retried, not surfaced.

    QA hit a raw `Claude API returned error status 529: {"type":"error",...}`
    on "Generate test plan" for what was a one-second blip on Anthropic's
    side. Every Claude call now goes through `_post_messages`, which backs
    off and retries the transient statuses, and reports whatever survives
    through `_status_error` in words a tester can act on.
    """

    @staticmethod
    def _client(monkeypatch):
        from src.app import llm_client as mod

        monkeypatch.setattr(mod.settings, "anthropic_api_key", "sk-test", raising=False)
        client = ClaudeClient()
        # Backoff without the wall-clock wait.
        monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock())
        return client

    @staticmethod
    def _responses(monkeypatch, statuses):
        """Stub the transport; return the list that records each attempt."""
        from src.app import llm_client as mod

        attempts = []

        class _StubClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, **kwargs):
                attempts.append(kwargs.get("json"))
                status = statuses[len(attempts) - 1]
                request = httpx.Request("POST", url)
                if status == 200:
                    return httpx.Response(
                        200,
                        request=request,
                        json={"content": [{"type": "text", "text": "ok"}]},
                    )
                return httpx.Response(status, request=request, text="Overloaded")

        monkeypatch.setattr(mod.httpx, "AsyncClient", _StubClient)
        return attempts

    @pytest.mark.asyncio
    async def test_529_is_retried_until_it_succeeds(self, monkeypatch):
        client = self._client(monkeypatch)
        attempts = self._responses(monkeypatch, [529, 529, 200])

        assert await client.summarize_ticket("A ticket", None) == "ok"
        assert len(attempts) == 3

    @pytest.mark.asyncio
    async def test_a_non_retryable_status_stops_the_loop(self, monkeypatch):
        client = self._client(monkeypatch)
        attempts = self._responses(monkeypatch, [529, 500, 200])

        with pytest.raises(LLMError):
            await client.summarize_batch([{"ticket_key": "SK-1", "summary": "x"}])
        assert len(attempts) == 2, "a 500 is not transient — don't burn attempts on it"

    @pytest.mark.asyncio
    async def test_plan_generation_goes_through_the_retrying_transport(self, monkeypatch):
        # The path QA actually hit. It used to make exactly one attempt and
        # surface the raw 529 body; assert against the transport it now uses
        # rather than standing up the whole prompt-building pipeline.
        client = self._client(monkeypatch)
        attempts = self._responses(monkeypatch, [529, 529, 200])

        data = await client._post_messages({"model": "m"}, timeout=600.0)
        assert data["content"][0]["text"] == "ok"
        assert len(attempts) == 3

    @pytest.mark.asyncio
    async def test_a_long_generation_does_not_re_run_on_timeout(self, monkeypatch):
        # Retrying a read timeout on a 10-minute plan call just burns minutes;
        # the short helpers opt in, this path doesn't.
        client = self._client(monkeypatch)
        calls = []

        class _Timeout:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                calls.append(1)
                raise httpx.ReadTimeout("too slow")

        from src.app import llm_client as mod
        monkeypatch.setattr(mod.httpx, "AsyncClient", _Timeout)

        with pytest.raises(httpx.ReadTimeout):
            await client._post_messages({"model": "m"}, timeout=600.0)
        assert len(calls) == 1

        calls.clear()
        with pytest.raises(httpx.ReadTimeout):
            await client._post_messages({"model": "m"}, timeout=60.0, retry_timeouts=True)
        assert len(calls) == 4

    @pytest.mark.asyncio
    async def test_persistent_overload_reports_in_plain_words(self, monkeypatch):
        client = self._client(monkeypatch)
        self._responses(monkeypatch, [529, 529, 529, 529])

        with pytest.raises(LLMError) as excinfo:
            await client.summarize_ticket("A ticket", None)
        assert "temporarily overloaded" in str(excinfo.value)
        assert "529" not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_best_effort_critic_retries_before_giving_up(self, monkeypatch):
        # These degrade to {} on error, so a throttled critic is invisible —
        # all the more reason for the retry to happen underneath it.
        client = self._client(monkeypatch)
        attempts = self._responses(monkeypatch, [529, 529, 529, 529])

        assert await client.verify_case_grounding([{"case_id": "hp-1", "title": "Save a listing"}]) == {}
        assert len(attempts) == 4

    def test_retry_after_header_wins_over_backoff(self):
        response = httpx.Response(529, headers={"retry-after": "7"})
        assert ClaudeClient._retry_delay(0, response) == 7.0

    def test_retry_after_is_capped(self):
        response = httpx.Response(529, headers={"retry-after": "9000"})
        assert ClaudeClient._retry_delay(0, response) == 30.0

    def test_backoff_grows_and_is_jittered(self):
        delays = [ClaudeClient._retry_delay(n, None) for n in range(3)]
        assert 1.0 <= delays[0] < 1.25
        assert 2.0 <= delays[1] < 2.5
        assert 4.0 <= delays[2] < 5.0

    def test_401_still_names_the_key_not_an_overload(self):
        e = httpx.HTTPStatusError(
            "401",
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            response=httpx.Response(401, json={"error": {"message": "invalid x-api-key"}}),
        )
        err = ClaudeClient._status_error(e)
        assert err.error_type == "invalid"
        assert "ANTHROPIC_API_KEY" in str(err)
