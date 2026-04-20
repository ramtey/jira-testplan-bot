"""
Tests for the Slack client used to resolve Slack permalinks found in Jira tickets.

Covers:
- parse_slack_url (non-thread, threaded, invalid)
- SlackClient.fetch_message (happy path, threaded, Slack API error)
- resolve_slack_messages_in_text (no token, dedup across description/comments,
  partial failures still return successes)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.models import SlackMessage
from src.app.slack_client import (
    SlackClient,
    parse_slack_url,
    resolve_slack_messages_in_text,
)


# ── parse_slack_url ────────────────────────────────────────────────────────────


def test_parse_slack_url_non_thread():
    out = parse_slack_url("https://acme.slack.com/archives/C012345/p1700000000123456")
    assert out == {
        "channel_id": "C012345",
        "ts": "1700000000.123456",
        "thread_ts": None,
    }


def test_parse_slack_url_thread():
    out = parse_slack_url(
        "https://acme.slack.com/archives/C012345/p1700000000123456"
        "?thread_ts=1699999999.999999&cid=C012345"
    )
    assert out["thread_ts"] == "1699999999.999999"
    assert out["ts"] == "1700000000.123456"
    assert out["channel_id"] == "C012345"


def test_parse_slack_url_invalid_returns_none():
    assert parse_slack_url("https://example.com/not-slack") is None
    assert parse_slack_url("https://acme.slack.com/team/U123") is None


# ── SlackClient.fetch_message ──────────────────────────────────────────────────


def _mock_response(payload: dict, status: int = 200):
    """Build a MagicMock that looks like an httpx Response."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def _patch_httpx_get(*ordered_responses):
    """Patch httpx.AsyncClient so successive .get() calls return these responses in order."""
    async_client_cm = MagicMock()
    client_instance = MagicMock()
    client_instance.get = AsyncMock(side_effect=list(ordered_responses))
    async_client_cm.__aenter__ = AsyncMock(return_value=client_instance)
    async_client_cm.__aexit__ = AsyncMock(return_value=None)
    return patch("src.app.slack_client.httpx.AsyncClient", return_value=async_client_cm)


@pytest.mark.asyncio
async def test_fetch_message_no_token_returns_none():
    """No configured token → bail out without calling the network."""
    client = SlackClient(token=None)
    with patch("src.app.slack_client.settings") as mock_settings:
        mock_settings.slack_user_token = None
        client.token = None
        result = await client.fetch_message(
            "https://acme.slack.com/archives/C1/p1700000000123456"
        )
    assert result is None


@pytest.mark.asyncio
async def test_fetch_message_non_thread_success():
    history_resp = _mock_response(
        {"ok": True, "messages": [{"ts": "1700000000.123456", "user": "U1", "text": "hello"}]}
    )
    users_resp = _mock_response(
        {"ok": True, "user": {"profile": {"display_name": "alice"}, "name": "alice"}}
    )

    with _patch_httpx_get(history_resp, users_resp):
        client = SlackClient(token="xoxp-test")
        msg = await client.fetch_message(
            "https://acme.slack.com/archives/C1/p1700000000123456"
        )

    assert isinstance(msg, SlackMessage)
    assert msg.author == "alice"
    assert msg.text == "hello"
    assert msg.ts == "1700000000.123456"
    assert msg.thread_ts is None


@pytest.mark.asyncio
async def test_fetch_message_thread_success():
    """Thread permalinks must locate the correct reply via conversations.replies."""
    replies_resp = _mock_response(
        {
            "ok": True,
            "messages": [
                {"ts": "1699999999.999999", "user": "U1", "text": "parent"},
                {"ts": "1700000000.123456", "user": "U2", "text": "the reply we want"},
            ],
        }
    )
    users_resp = _mock_response(
        {"ok": True, "user": {"profile": {"display_name": "bob"}}}
    )

    with _patch_httpx_get(replies_resp, users_resp):
        client = SlackClient(token="xoxp-test")
        msg = await client.fetch_message(
            "https://acme.slack.com/archives/C1/p1700000000123456"
            "?thread_ts=1699999999.999999&cid=C1"
        )

    assert msg is not None
    assert msg.text == "the reply we want"
    assert msg.thread_ts == "1699999999.999999"
    assert msg.author == "bob"


@pytest.mark.asyncio
async def test_fetch_message_slack_api_error_returns_none():
    """Slack returns 200 with ok=false for auth/permission errors — must degrade to None."""
    error_resp = _mock_response({"ok": False, "error": "channel_not_found"})

    with _patch_httpx_get(error_resp):
        client = SlackClient(token="xoxp-test")
        msg = await client.fetch_message(
            "https://acme.slack.com/archives/C1/p1700000000123456"
        )
    assert msg is None


# ── resolve_slack_messages_in_text ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolver_returns_empty_without_token(monkeypatch):
    monkeypatch.setattr("src.app.slack_client.settings.slack_user_token", None)
    with patch("src.app.slack_client.SlackClient") as mock_client_cls:
        result = await resolve_slack_messages_in_text(
            "see https://acme.slack.com/archives/C1/p1700000000123456",
            [{"body": "also https://acme.slack.com/archives/C2/p1800000000999999"}],
        )
    assert result == []
    # No client instantiation at all when token missing.
    mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_resolver_dedups_and_fetches_across_description_and_comments(monkeypatch):
    monkeypatch.setattr("src.app.slack_client.settings.slack_user_token", "xoxp-test")

    desc = (
        "initial link https://acme.slack.com/archives/C1/p1700000000111111 "
        "and a duplicate https://acme.slack.com/archives/C1/p1700000000111111."
    )
    comments = [
        {"body": "new one https://acme.slack.com/archives/C2/p1800000000222222"},
        {"body": "no links here"},
    ]

    fake_client = MagicMock()

    async def fake_fetch(url):
        return SlackMessage(
            url=url, channel_id="C", ts="x", author="a", text="t", thread_ts=None
        )

    fake_client.fetch_message = AsyncMock(side_effect=fake_fetch)
    with patch("src.app.slack_client.SlackClient", return_value=fake_client):
        result = await resolve_slack_messages_in_text(desc, comments)

    # Duplicate URL in description collapses to 1; plus the comment URL = 2 total.
    assert len(result) == 2
    assert fake_client.fetch_message.await_count == 2
    called_urls = {call.args[0] for call in fake_client.fetch_message.await_args_list}
    assert called_urls == {
        "https://acme.slack.com/archives/C1/p1700000000111111",
        "https://acme.slack.com/archives/C2/p1800000000222222",
    }


@pytest.mark.asyncio
async def test_resolver_drops_individual_failures(monkeypatch):
    """One unreachable URL must not prevent the others from being returned."""
    monkeypatch.setattr("src.app.slack_client.settings.slack_user_token", "xoxp-test")

    desc = (
        "good https://acme.slack.com/archives/C1/p1700000000111111 "
        "bad https://acme.slack.com/archives/C2/p1800000000222222"
    )

    async def fake_fetch(url):
        if "C2" in url:
            return None  # simulate API error for this one
        return SlackMessage(
            url=url, channel_id="C1", ts="x", author="a", text="t", thread_ts=None
        )

    fake_client = MagicMock()
    fake_client.fetch_message = AsyncMock(side_effect=fake_fetch)
    with patch("src.app.slack_client.SlackClient", return_value=fake_client):
        result = await resolve_slack_messages_in_text(desc, [])

    assert len(result) == 1
    assert result[0].channel_id == "C1"
