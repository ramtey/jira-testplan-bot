"""
Bug Lens routes — analyze bug tickets to explain root cause, fix, and regression tests.

Mounted at /bug-lens in main.py via APIRouter.
"""

import re
from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from .config import settings
from .github_client import GitHubClient
from .llm_client import LLMError, get_llm_client
from .models import BugAnalysisRequest, MultiBugAnalysisRequest

_BLOB_PATTERN = re.compile(
    r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/blob/[^\s>\"'\)]+"
)
_COMMIT_PATTERN = re.compile(
    r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/commit/[0-9a-f]{7,40}"
)


async def _fetch_github_context(description: str | None, comments: list[dict] | None) -> list[dict] | None:
    """
    Scan ticket description and comments for GitHub file/commit URLs and fetch their content.

    Only runs when a GitHub token is configured. Returns None if nothing was found or fetched.
    Caps at 3 file URLs and 2 commit URLs to keep prompt size reasonable.
    """
    if not settings.github_token:
        return None

    texts = [description or ""]
    for c in (comments or []):
        texts.append(c.get("body", ""))
    combined = "\n".join(texts)

    blob_urls = list({m.group(0).rstrip(".,;)>]\"'") for m in _BLOB_PATTERN.finditer(combined)})[:3]
    commit_urls = list({m.group(0).rstrip(".,;)>]\"'") for m in _COMMIT_PATTERN.finditer(combined)})[:2]

    if not blob_urls and not commit_urls:
        return None

    client = GitHubClient()
    context: list[dict] = []

    for url in blob_urls:
        result = await client.fetch_file_from_blob_url(url)
        if result:
            context.append({"type": "file", "url": url, **result})

    for url in commit_urls:
        result = await client.fetch_commit_from_url(url)
        if result:
            context.append({"type": "commit", "url": url, **result})

    return context or None

router = APIRouter(prefix="/bug-lens", tags=["bug-lens"])


@router.post("/analyze")
async def analyze_bug(request: BugAnalysisRequest):
    """
    Analyze a single bug ticket.

    Uses the configured LLM to explain the bug, identify root cause,
    describe the fix (if a merged PR exists), and suggest regression tests.
    """
    try:
        llm = get_llm_client()
        github_context = await _fetch_github_context(request.description, request.comments)
        analysis = await llm.generate_bug_analysis(
            ticket_key=request.ticket_key,
            summary=request.summary,
            description=request.description,
            development_info=request.development_info,
            comments=request.comments,
            linked_info=request.linked_info,
            github_context=github_context,
        )
        return {
            "ticket_key": request.ticket_key,
            **asdict(analysis),
        }
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/analyze/multi")
async def analyze_bugs_multi(request: MultiBugAnalysisRequest):
    """
    Analyze multiple related bug tickets together.

    Produces a single combined analysis covering the shared root cause,
    fix explanation, and regression tests across all tickets.
    """
    tickets_data = []
    for t in request.tickets:
        github_context = await _fetch_github_context(t.description, t.comments)
        tickets_data.append({
            "ticket_key": t.ticket_key,
            "summary": t.summary,
            "description": t.description,
            "development_info": t.development_info,
            "comments": t.comments,
            "linked_info": t.linked_info,
            "github_context": github_context,
        })

    try:
        llm = get_llm_client()
        analysis = await llm.generate_multi_bug_analysis(tickets=tickets_data)
        return {
            "ticket_keys": [t.ticket_key for t in request.tickets],
            **asdict(analysis),
        }
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e))
