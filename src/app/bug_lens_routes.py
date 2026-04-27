"""
Bug Lens routes — analyze bug tickets to explain root cause, fix, and regression tests.

Mounted at /bug-lens in main.py via APIRouter.
"""

import logging
import re
from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from .config import settings
from .github_client import GitHubClient
from .llm_client import LLMError, get_llm_client
from .models import BugAnalysisRequest, MultiBugAnalysisRequest

logger = logging.getLogger(__name__)

_BLOB_PATTERN = re.compile(
    r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/blob/[^\s>\"'\)]+"
)
_COMMIT_PATTERN = re.compile(
    r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/commit/[0-9a-f]{7,40}"
)

# Built-in product keyword → repo mapping. Each entry maps a regex (case-insensitive)
# to one or more "owner/repo" strings. Keywords are matched against the ticket's
# summary + description + comments, and every matching entry contributes its repos
# (deduplicated). Users can extend this without code changes via the
# BUG_LENS_REPO_HINTS env var (see config.py).
_TITLE_REPO_MAP: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"agent.?cal(culator)?", re.IGNORECASE), ["skyslope/agent-calculator"]),
    (re.compile(r"\bayce\b", re.IGNORECASE), ["skyslope/agent-coach"]),
]

# How many matched repos to actually search per ticket, as a cost/noise cap.
_MAX_REPOS_TO_SEARCH = 3

# Stop words excluded when building a code search query from a ticket summary
_STOP_WORDS = {
    "a", "an", "the", "is", "in", "on", "at", "to", "for", "of", "and", "or",
    "not", "with", "when", "that", "this", "it", "be", "are", "was", "were",
    "has", "have", "had", "does", "do", "did", "by", "as", "but", "from",
    "bug", "fix", "issue", "error", "fail", "incorrect", "wrong", "broken",
}


def _infer_repos_from_text(*texts: str | None) -> list[str]:
    """
    Return all owner/repo strings whose keyword patterns match any of the given texts.

    Scans the concatenation of the provided texts (summary, description, comments)
    against both the built-in _TITLE_REPO_MAP and user-provided hints from
    settings.bug_lens_repo_hints. Returns a deduplicated list preserving the order
    in which repos first appeared.
    """
    combined = "\n".join(t for t in texts if t)
    if not combined:
        return []

    seen: set[str] = set()
    result: list[str] = []

    def _add(repos: list[str]) -> None:
        for r in repos:
            if r and r not in seen:
                seen.add(r)
                result.append(r)

    for pattern, repos in _TITLE_REPO_MAP:
        if pattern.search(combined):
            _add(repos)

    for pattern_str, repos in (settings.bug_lens_repo_hints or {}).items():
        try:
            if re.search(pattern_str, combined, re.IGNORECASE):
                _add(repos)
        except re.error as e:
            logger.warning(f"Invalid regex in bug_lens_repo_hints: {pattern_str!r} ({e})")

    return result


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


_REPO_FROM_URL_PATTERN = re.compile(
    r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(?:blob|commit|pull|tree)/"
)


def _infer_repos_for_evidence(
    summary: str,
    description: str | None,
    comments: list[dict] | None,
) -> list[str]:
    """
    Infer candidate repos for code-evidence search: first from any GitHub URLs
    in the ticket body, then from the existing keyword map. Preserves order,
    deduplicates.
    """
    texts = [summary or "", description or ""]
    for c in (comments or []):
        texts.append(c.get("body", ""))
    combined = "\n".join(texts)

    seen: set[str] = set()
    result: list[str] = []

    for match in _REPO_FROM_URL_PATTERN.finditer(combined):
        repo = f"{match.group(1)}/{match.group(2)}"
        if repo not in seen:
            seen.add(repo)
            result.append(repo)

    comment_bodies = [c.get("body", "") for c in (comments or [])]
    for repo in _infer_repos_from_text(summary, description, *comment_bodies):
        if repo not in seen:
            seen.add(repo)
            result.append(repo)

    return result


def _find_symbol_line(content: str, symbol: str) -> tuple[int, str] | None:
    """Return (line_number, trimmed_snippet) for the first line containing the symbol."""
    for i, line in enumerate(content.splitlines(), start=1):
        if symbol in line:
            trimmed = line.strip()
            if len(trimmed) > 200:
                trimmed = trimmed[:200] + "..."
            return i, trimmed
    return None


_DOC_EXTENSIONS = (".md", ".mdx", ".rst", ".txt")


async def _compute_code_evidence(
    suspect_symbols: list[str] | None,
    repos: list[str],
) -> list[dict]:
    """
    Run a deterministic GitHub code search for each suspect symbol across the
    candidate repos, and package the hits into a code_evidence list.

    Returns [] (not None) when nothing can be searched — the route decides
    whether to set the field based on whether search was even attempted.

    Doc files (.md/.mdx/.rst/.txt) are filtered out — text search matches
    them whenever a symbol is mentioned in a README, and they're almost
    never what the reviewer wants in a "Code Evidence" section.
    """
    if not suspect_symbols or not repos or not settings.github_token:
        return []

    client = GitHubClient()
    evidence: list[dict] = []

    for symbol in suspect_symbols[:3]:
        for repo in repos[:_MAX_REPOS_TO_SEARCH]:
            try:
                files = await client.search_relevant_files(repo, symbol, max_files=5)
            except Exception as e:
                logger.warning(f"Code evidence search failed for {symbol!r} in {repo}: {e}")
                files = []

            usages: list[dict] = []
            for f in files:
                path = f.get("path", "")
                if path.lower().endswith(_DOC_EXTENSIONS):
                    continue
                hit = _find_symbol_line(f.get("content", ""), symbol)
                if hit is None:
                    continue
                line_no, snippet = hit
                usages.append({
                    "path": path,
                    "ref": f.get("ref"),
                    "line": line_no,
                    "snippet": snippet,
                })

            evidence.append({
                "suspect": symbol,
                "repo": repo,
                "usages": usages,
                "notes": None if usages else "No matches found in this repo.",
            })

    return evidence


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
        # No explicit links — try inferring repos from keywords in the full ticket text
        comment_bodies = [c.get("body", "") for c in (comments or [])]
        repos = _infer_repos_from_text(summary, description, *comment_bodies)
        if not repos:
            logger.info(
                "Bug Lens: no repo keywords matched for summary %r — "
                "add entries to BUG_LENS_REPO_HINTS to extend coverage.",
                summary,
            )
        else:
            query = _build_search_query(summary)
            if query:
                seen_paths: set[tuple[str, str]] = set()
                for repo in repos[:_MAX_REPOS_TO_SEARCH]:
                    files = await client.search_relevant_files(repo, query)
                    for f in files:
                        key = (repo, f.get("path", ""))
                        if key in seen_paths:
                            continue
                        seen_paths.add(key)
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
            status=request.status,
            status_category=request.status_category,
        )
        repos = _infer_repos_for_evidence(request.summary, request.description, request.comments)
        evidence = await _compute_code_evidence(analysis.suspect_symbols, repos)
        if evidence:
            analysis.code_evidence = evidence
        return {
            "ticket_key": request.ticket_key,
            **asdict(analysis),
            "is_fixed": analysis.fix_status == "fixed",
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
            "status": t.status,
            "status_category": t.status_category,
        })

    try:
        llm = get_llm_client()
        analysis = await llm.generate_multi_bug_analysis(tickets=tickets_data)
        combined_repos: list[str] = []
        for t in request.tickets:
            for repo in _infer_repos_for_evidence(t.summary, t.description, t.comments):
                if repo not in combined_repos:
                    combined_repos.append(repo)
        evidence = await _compute_code_evidence(analysis.suspect_symbols, combined_repos)
        if evidence:
            analysis.code_evidence = evidence
        return {
            "ticket_keys": [t.ticket_key for t in request.tickets],
            **asdict(analysis),
            "is_fixed": analysis.fix_status == "fixed",
        }
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e))
