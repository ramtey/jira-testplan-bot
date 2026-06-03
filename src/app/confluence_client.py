"""
Confluence client — fetches linked spec pages referenced from a Jira ticket.

Atlassian Cloud puts Jira and Confluence on the same `<site>.atlassian.net`
domain and accepts the same Basic auth, so we reuse the Jira credentials
instead of introducing a separate token.

Scope is intentionally narrow for the first iteration: extract page IDs from
URLs that appear in the supplied text (description + comments), fetch each
page's storage body, strip the XHTML to plain text, and hand the result back
to the LLM prompt builder. We do not (yet) resolve Confluence shortlinks
(`/wiki/x/<id>`) or follow Jira remote-issue-link entries.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass

import httpx

from .config import settings

logger = logging.getLogger(__name__)


_CONFLUENCE_URL_RE = re.compile(
    r"https?://[\w.-]+\.atlassian\.net/wiki/spaces/[^/\s]+/pages/(\d+)(?:/[^\s)\]]*)?",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# Caps tuned to keep prompt growth predictable. Pages can be enormous; three
# specs at 8k chars each is ~24k chars of additional prompt — large but well
# under the budget for Opus and small enough that we won't blow context on a
# ticket with a chatty Confluence link.
MAX_PAGES_PER_TICKET = 3
MAX_BODY_CHARS = 8000


@dataclass
class ConfluencePage:
    page_id: str
    url: str
    title: str
    body_text: str


def extract_confluence_page_ids(text: str) -> list[tuple[str, str]]:
    """Return (page_id, full_url) pairs found in `text`, de-duplicated, order preserved."""
    if not text:
        return []
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for match in _CONFLUENCE_URL_RE.finditer(text):
        page_id = match.group(1)
        if page_id in seen:
            continue
        seen.add(page_id)
        pairs.append((page_id, match.group(0)))
    return pairs


def _strip_storage_xhtml(html: str) -> str:
    """Collapse Confluence storage-format XHTML to readable plain text."""
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    return _WHITESPACE_RE.sub(" ", text).strip()


class ConfluenceClient:
    def __init__(self) -> None:
        self.base_url = settings.jira_url.rstrip("/")
        auth_bytes = f"{settings.jira_username}:{settings.jira_api_token}".encode("utf-8")
        self._auth_header = base64.b64encode(auth_bytes).decode("utf-8")

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Basic {self._auth_header}",
        }

    async def _fetch_page(self, client: httpx.AsyncClient, page_id: str, url: str) -> ConfluencePage | None:
        api_url = f"{self.base_url}/wiki/api/v2/pages/{page_id}"
        try:
            r = await client.get(api_url, headers=self._headers(), params={"body-format": "storage"})
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning(f"Confluence fetch failed for {page_id}: {exc}")
            return None
        if r.status_code in (401, 403):
            # Confluence permissions are independent of Jira — a Jira-readable
            # ticket can link a Confluence page the same user can't open.
            # Treat as missing rather than fatal so the test plan still
            # generates from the rest of the context.
            logger.info(f"Confluence page {page_id} not accessible (HTTP {r.status_code})")
            return None
        if r.status_code == 404:
            logger.info(f"Confluence page {page_id} not found")
            return None
        if r.status_code >= 400:
            logger.warning(f"Confluence page {page_id} returned HTTP {r.status_code}")
            return None
        data = r.json()
        title = data.get("title") or f"Page {page_id}"
        body_xhtml = ((data.get("body") or {}).get("storage") or {}).get("value") or ""
        body_text = _strip_storage_xhtml(body_xhtml)
        if len(body_text) > MAX_BODY_CHARS:
            body_text = body_text[:MAX_BODY_CHARS] + " …(truncated)"
        return ConfluencePage(page_id=page_id, url=url, title=title, body_text=body_text)

    async def fetch_pages_from_text(self, text: str) -> list[ConfluencePage]:
        """Find Confluence URLs in `text` and return the fetched page contents."""
        if not settings.jira_url or not settings.jira_api_token:
            return []
        pairs = extract_confluence_page_ids(text)[:MAX_PAGES_PER_TICKET]
        if not pairs:
            return []
        pages: list[ConfluencePage] = []
        async with httpx.AsyncClient(timeout=15) as client:
            for page_id, url in pairs:
                page = await self._fetch_page(client, page_id, url)
                if page and page.body_text:
                    pages.append(page)
        if pages:
            logger.info(f"Fetched {len(pages)} Confluence spec page(s) for prompt enrichment")
        return pages
