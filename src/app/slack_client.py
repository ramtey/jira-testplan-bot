"""
Slack API client for resolving Slack message links found in Jira tickets.

Fetches a single message (and its author) referenced by a Slack permalink such as:
- https://workspace.slack.com/archives/C012345/p1700000000123456
- https://workspace.slack.com/archives/C012345/p1700000000123456?thread_ts=1699999999.999999&cid=C012345

All failure modes return None and log a warning — callers are expected to degrade
gracefully (e.g. fall through to showing the raw URL).
"""

import logging
import re
from urllib.parse import parse_qs

import httpx

from .config import settings
from .models import SlackMessage

logger = logging.getLogger(__name__)


SLACK_URL_PATTERN = re.compile(
    r"https?://[a-zA-Z0-9-]+\.slack\.com/archives/([A-Z0-9]+)/p(\d+)(?:\?([^\s]+))?"
)


async def resolve_slack_messages_in_text(
    description: str | None,
    comments: list[dict] | None,
) -> list[SlackMessage]:
    """Scan ticket text for Slack permalinks and fetch each message.

    Returns [] when no token is configured or no permalinks are found; never
    raises. Individual fetch failures are dropped so one broken link does not
    block the rest.
    """
    if not settings.slack_user_token:
        return []

    found_urls: set[str] = set()

    if description:
        for match in SLACK_URL_PATTERN.finditer(description):
            found_urls.add(match.group(0).rstrip(".,;)>]\"'"))

    for comment in comments or []:
        body = comment.get("body") or ""
        for match in SLACK_URL_PATTERN.finditer(body):
            found_urls.add(match.group(0).rstrip(".,;)>]\"'"))

    if not found_urls:
        return []

    client = SlackClient()
    messages: list[SlackMessage] = []
    for url in found_urls:
        msg = await client.fetch_message(url)
        if msg:
            messages.append(msg)

    if messages:
        logger.info(f"Resolved {len(messages)}/{len(found_urls)} Slack message(s) from ticket text")
    return messages


def parse_slack_url(url: str) -> dict | None:
    """Parse a Slack permalink into channel_id, ts, and optional thread_ts.

    Returns None if the URL is not a recognizable Slack archive link.
    """
    match = SLACK_URL_PATTERN.match(url)
    if not match:
        return None

    channel_id = match.group(1)
    p_ts = match.group(2)
    query = match.group(3)

    # p1700000000123456 -> 1700000000.123456 (Slack's permalink strips the dot).
    if len(p_ts) < 7:
        return None
    ts = f"{p_ts[:-6]}.{p_ts[-6:]}"

    thread_ts = None
    if query:
        thread_ts_vals = parse_qs(query).get("thread_ts")
        if thread_ts_vals:
            thread_ts = thread_ts_vals[0]

    return {"channel_id": channel_id, "ts": ts, "thread_ts": thread_ts}


class SlackClient:
    """Client for resolving individual Slack messages via the Web API."""

    def __init__(self, token: str | None = None):
        self.token = token or settings.slack_user_token
        self.base_url = "https://slack.com/api"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }

    async def fetch_message(self, slack_url: str) -> SlackMessage | None:
        """Fetch a single Slack message referenced by a permalink.

        Returns None when the token is missing, the URL is unparseable, or the
        Slack API returns an error. Errors are logged as warnings, never raised.
        """
        if not self.token:
            logger.warning("Slack user token not configured - skipping Slack message fetch")
            return None

        parsed = parse_slack_url(slack_url)
        if not parsed:
            logger.warning(f"Could not parse Slack URL: {slack_url}")
            return None

        channel_id = parsed["channel_id"]
        ts = parsed["ts"]
        thread_ts = parsed["thread_ts"]

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                message = await self._fetch_raw_message(client, channel_id, ts, thread_ts)
                if not message:
                    return None

                author = await self._resolve_author_from_message(client, message)
                text = message.get("text", "") or ""

                return SlackMessage(
                    url=slack_url,
                    channel_id=channel_id,
                    ts=ts,
                    thread_ts=thread_ts,
                    author=author,
                    text=text,
                )
        except httpx.TimeoutException:
            logger.warning(f"Timeout fetching Slack message: {slack_url}")
            return None
        except Exception as e:
            logger.warning(f"Unexpected error fetching Slack message {slack_url}: {e}")
            return None

    async def _fetch_raw_message(
        self,
        client: httpx.AsyncClient,
        channel_id: str,
        ts: str,
        thread_ts: str | None,
    ) -> dict | None:
        """Call conversations.history or conversations.replies to locate one message."""
        if thread_ts:
            url = f"{self.base_url}/conversations.replies"
            params = {
                "channel": channel_id,
                "ts": thread_ts,
                "inclusive": "true",
                "limit": "200",
            }
        else:
            url = f"{self.base_url}/conversations.history"
            params = {
                "channel": channel_id,
                "latest": ts,
                "oldest": ts,
                "inclusive": "true",
                "limit": "1",
            }

        response = await client.get(url, headers=self._headers(), params=params)
        if response.status_code != 200:
            logger.warning(f"Slack API returned status {response.status_code} for {url}")
            return None

        data = response.json()
        if not data.get("ok"):
            logger.warning(f"Slack API error for channel {channel_id}: {data.get('error')}")
            return None

        for msg in data.get("messages", []):
            if msg.get("ts") == ts:
                return msg

        logger.warning(f"Slack message ts={ts} not found in channel {channel_id}")
        return None

    async def _resolve_author_from_message(
        self, client: httpx.AsyncClient, message: dict
    ) -> str | None:
        """Derive a human-readable author from a message payload."""
        if message.get("username"):
            return message["username"]
        user_id = message.get("user")
        if user_id:
            return await self._resolve_user(client, user_id)
        bot_id = message.get("bot_id")
        if bot_id:
            return f"bot:{bot_id}"
        return None

    async def _resolve_user(self, client: httpx.AsyncClient, user_id: str) -> str:
        """Resolve a Slack user ID to a display name, falling back to the ID."""
        try:
            response = await client.get(
                f"{self.base_url}/users.info",
                headers=self._headers(),
                params={"user": user_id},
            )
            if response.status_code != 200:
                return user_id
            data = response.json()
            if not data.get("ok"):
                return user_id
            profile = data.get("user", {}).get("profile", {})
            return (
                profile.get("display_name")
                or profile.get("real_name")
                or data.get("user", {}).get("name")
                or user_id
            )
        except Exception:
            return user_id
