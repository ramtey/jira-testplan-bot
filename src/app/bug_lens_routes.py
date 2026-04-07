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

# Map title keywords (case-insensitive) to GitHub repos.
# Checked in order — first match wins.
_TITLE_REPO_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"agent.?cal(culator)?", re.IGNORECASE), "skyslope/agent-calculator"),
    (re.compile(r"\bayce\b", re.IGNORECASE), "skyslope/agent-coach"),
]

# Stop words excluded when building a code search query from a ticket summary
_STOP_WORDS = {
    "a", "an", "the", "is", "in", "on", "at", "to", "for", "of", "and", "or",
    "not", "with", "when", "that", "this", "it", "be", "are", "was", "were",
    "has", "have", "had", "does", "do", "did", "by", "as", "but", "from",
    "bug", "fix", "issue", "error", "fail", "incorrect", "wrong", "broken",
}


def _infer_repo_from_summary(summary: str) -> str | None:
    """Return the owner/repo inferred from title keywords, or None if no match."""
    for pattern, repo in _TITLE_REPO_MAP:
        if pattern.search(summary):
            return repo
    return None


def _build_search_query(summary: str) -> str:
    """Extract meaningful terms from a ticket summary for use as a code search query."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9]*", summary)
    terms = [w for w in words if len(w) > 3 and w.lower() not in _STOP_WORDS]
    # Deduplicate while preserving order, cap at 6 terms
    seen: set[str] = set()
    unique: list[str] = []
    for t in terms:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            unique.append(t)
        if len(unique) == 6:
            break
    return " ".join(unique)


async def _fetch_github_context(
    summary: str,
    description: str | None,
    comments: list[dict] | None,
) -> list[dict] | None:
    """
    Build GitHub code context for bug analysis.

    First scans ticket description and comments for explicit GitHub blob/commit URLs
    and fetches their content. If nothing is found, falls back to inferring the repo
    from the ticket summary (via _TITLE_REPO_MAP) and running a code search.

    Only runs when a GitHub token is configured.
    """
    if not settings.github_token:
        return None

    texts = [description or ""]
    for c in (comments or []):
        texts.append(c.get("body", ""))
    combined = "\n".join(texts)

    blob_urls = list({m.group(0).rstrip(".,;)>]\"'") for m in _BLOB_PATTERN.finditer(combined)})[:3]
    commit_urls = list({m.group(0).rstrip(".,;)>]\"'") for m in _COMMIT_PATTERN.finditer(combined)})[:2]

    client = GitHubClient()
    context: list[dict] = []

    if blob_urls or commit_urls:
        for url in blob_urls:
            result = await client.fetch_file_from_blob_url(url)
            if result:
                context.append({"type": "file", "url": url, **result})

        for url in commit_urls:
            result = await client.fetch_commit_from_url(url)
            if result:
                context.append({"type": "commit", "url": url, **result})
    else:
        # No explicit links — try inferring the repo from the ticket title
        repo = _infer_repo_from_summary(summary)
        if repo:
            query = _build_search_query(summary)
            if query:
                files = await client.search_relevant_files(repo, query)
                for f in files:
                    context.append({"type": "file", "repo": repo, **f})

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
        github_context = await _fetch_github_context(request.summary, request.description, request.comments)
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
        github_context = await _fetch_github_context(t.summary, t.description, t.comments)
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
