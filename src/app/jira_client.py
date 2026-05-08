import base64
import logging
import re

import httpx

from .adf_parser import extract_text_from_adf
from .config import settings
from .description_analyzer import analyze_description
from .figma_client import FigmaClient
from .github_client import GitHubClient
from .models import Attachment, BounceEvent, Commit, DevelopmentInfo, DescriptionAnalysis, EpicChildSummary, FileChange, JiraComment, JiraIssue, LinkedIssue, LinkedIssues, ParentIssue, PRComment, PullRequest, RepositoryContext

logger = logging.getLogger(__name__)


# Source of truth: skyslope/agent-calculator -> agent-calculator-docs/Team Members.md
# (introduced in PR #532). GitHub display names are free-text and don't match
# Jira display names, and several team members route commits through GitHub
# noreply emails — login-based mapping is the only reliable signal. Keep this
# in sync with the upstream doc when the team changes.
TEAM_GITHUB_LOGIN_TO_JIRA: dict[str, tuple[str, str]] = {
    "steviecs": ("5b15aed34d941a51f0da4491", "Steven Sullivan"),
    "piradukunda": ("712020:0fac97f2-6ad3-4c72-9cf8-a73d1ee9ba83", "Patrick Iradukunda"),
    "ramtey": ("557058:37d141e3-d541-42c5-9b2a-79278da6598e", "Ramtin Teymouri"),
    "kszombathy-skyslope": ("633b1ba7409249995eeb9578", "Kyle Szombathy"),
    "ssteuteville": ("712020:79959462-f899-4f82-9a2d-522c45cefaa0", "Shane Steuteville"),
}

# Bot accounts that must never be the final assignee on pass-to-UAT or
# fail-to-todo, regardless of which lookup surfaced them. Compared
# case-insensitively against Jira display names. Belt-and-suspenders for
# the accountId-based safety net: catches stale credentials, additional
# bot accounts, or any path that returns a name we recognize as a bot.
BOT_DISPLAY_NAME_BLOCKLIST: frozenset[str] = frozenset({"testing skyslope"})


def is_blocked_bot_display_name(name: str | None) -> bool:
    """Return True if `name` matches a known bot account (case-insensitive)."""
    return bool(name) and name.strip().lower() in BOT_DISPLAY_NAME_BLOCKLIST


def _parse_inline_markdown(text: str) -> list:
    """Convert inline markdown (bold, italic, code) to ADF text nodes."""
    nodes = []
    pattern = r'(\*\*(.+?)\*\*|__(.+?)__|`(.+?)`|\*(.+?)\*|_(.+?)_|([^*_`]+))'
    for match in re.finditer(pattern, text, re.DOTALL):
        full = match.group(0)
        if full.startswith('**') or full.startswith('__'):
            inner = match.group(2) or match.group(3)
            nodes.append({"type": "text", "text": inner, "marks": [{"type": "strong"}]})
        elif full.startswith('`'):
            nodes.append({"type": "text", "text": match.group(4), "marks": [{"type": "code"}]})
        elif full.startswith('*') or full.startswith('_'):
            inner = match.group(5) or match.group(6)
            nodes.append({"type": "text", "text": inner, "marks": [{"type": "em"}]})
        elif full:
            nodes.append({"type": "text", "text": full})
    return nodes or [{"type": "text", "text": text}]


def markdown_to_adf(markdown_text: str) -> dict:
    """Convert markdown text to Atlassian Document Format (ADF)."""
    lines = markdown_text.split('\n')
    content = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Fenced code block
        if line.startswith('```'):
            lang = line[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith('```'):
                code_lines.append(lines[i])
                i += 1
            content.append({
                "type": "codeBlock",
                "attrs": {"language": lang or ""},
                "content": [{"type": "text", "text": '\n'.join(code_lines)}]
            })
            i += 1
            continue

        # Heading
        if line.startswith('#'):
            level = len(line) - len(line.lstrip('#'))
            level = min(level, 6)
            text = line[level:].strip()
            content.append({
                "type": "heading",
                "attrs": {"level": level},
                "content": _parse_inline_markdown(text)
            })
            i += 1
            continue

        # Horizontal rule
        stripped = line.strip()
        if re.match(r'^(-{3,}|\*{3,}|_{3,})$', stripped):
            content.append({"type": "rule"})
            i += 1
            continue

        # Bullet list (collect consecutive items)
        if re.match(r'^[-*] ', line):
            items = []
            while i < len(lines) and re.match(r'^[-*] ', lines[i]):
                item_text = lines[i][2:].strip()
                items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": _parse_inline_markdown(item_text)}]
                })
                i += 1
            content.append({"type": "bulletList", "content": items})
            continue

        # Ordered list (collect consecutive items)
        if re.match(r'^\d+\. ', line):
            items = []
            while i < len(lines) and re.match(r'^\d+\. ', lines[i]):
                item_text = re.sub(r'^\d+\. ', '', lines[i]).strip()
                items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": _parse_inline_markdown(item_text)}]
                })
                i += 1
            content.append({"type": "orderedList", "content": items})
            continue

        # Skip blank lines
        if not stripped:
            i += 1
            continue

        # Regular paragraph
        content.append({"type": "paragraph", "content": _parse_inline_markdown(line)})
        i += 1

    return {
        "type": "doc",
        "version": 1,
        "content": content or [{"type": "paragraph", "content": [{"type": "text", "text": ""}]}]
    }


TEST_PLAN_MARKER = "🤖 Generated Test Plan"
TEST_PLAN_EXPAND_TITLE = "Click to view"

QA_PASS_MARKER = "✅ QA Passed — ready for UAT"
QA_PASS_EXPAND_TITLE = "Test summary"
QA_FAIL_MARKER = "❌ QA Failed — back to To Do"


def _normalize_environments(environments: list[str] | None) -> list[str]:
    """Trim, drop empties, and dedupe while preserving order."""
    if not environments:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for env in environments:
        if not isinstance(env, str):
            continue
        env = env.strip()
        if not env or env.lower() in seen:
            continue
        seen.add(env.lower())
        out.append(env)
    return out


def _build_qa_pass_adf(
    loom_url: str | None,
    summary: str | None,
    environments: list[str] | None = None,
    mention_account_ids: list[str] | None = None,
    image_urls: list[str] | None = None,
) -> dict | None:
    """Build the ADF body for a QA→UAT pass comment.

    The marker paragraph is always first so future flows can detect this
    comment the same way test-plan comments are detected. The environments
    tag (e.g. `(Integ + Staging)`) is rendered into the marker line so it
    stays visible without expanding. The Loom link sits above the fold,
    image links sit just below it (also above-fold so reviewers see proof
    without expanding), and the freeform summary is wrapped in an `expand`
    node. When mentions are supplied, a final "cc:" paragraph triggers
    Jira notifications.

    Mentions alone don't justify posting a comment — the function still
    returns None unless at least one substantive field
    (loom/summary/envs/images) is populated, so QA accidentally selecting
    a chip with no other content stays a one-click pass.
    """
    loom_url = (loom_url or "").strip()
    summary = (summary or "").strip()
    envs = _normalize_environments(environments)
    images = _normalize_url_list(image_urls)
    if not loom_url and not summary and not envs and not images:
        return None

    if envs:
        marker_text = QA_PASS_MARKER.replace(
            "QA Passed", f"QA Passed ({' + '.join(envs)})"
        )
    else:
        marker_text = QA_PASS_MARKER

    content: list[dict] = [
        {"type": "paragraph", "content": [{"type": "text", "text": marker_text}]}
    ]

    if loom_url:
        content.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "📹 Loom: "},
                {
                    "type": "text",
                    "text": loom_url,
                    "marks": [{"type": "link", "attrs": {"href": loom_url}}],
                },
            ],
        })

    for url in images:
        content.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "🖼️ "},
                {
                    "type": "text",
                    "text": url,
                    "marks": [{"type": "link", "attrs": {"href": url}}],
                },
            ],
        })

    if summary:
        summary_doc = markdown_to_adf(summary)
        content.append({
            "type": "expand",
            "attrs": {"title": QA_PASS_EXPAND_TITLE},
            "content": summary_doc.get("content", []),
        })

    mentions_para = _build_mentions_paragraph(mention_account_ids)
    if mentions_para:
        content.append(mentions_para)

    return {"type": "doc", "version": 1, "content": content}


def _normalize_url_list(urls: list[str] | None) -> list[str]:
    if not urls:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if not isinstance(url, str):
            continue
        url = url.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _build_mentions_paragraph(account_ids: list[str] | None) -> dict | None:
    """Render an ADF paragraph that @mentions each accountId.

    Jira notifies a user when their accountId appears as a `mention` node
    in a comment body. Returns None when the input is empty so callers
    can drop the paragraph entirely.
    """
    ids = _normalize_url_list(account_ids)  # Same shape: trim, dedupe, drop blanks.
    if not ids:
        return None
    nodes: list[dict] = [{"type": "text", "text": "cc: "}]
    for i, account_id in enumerate(ids):
        if i > 0:
            nodes.append({"type": "text", "text": " "})
        nodes.append({"type": "mention", "attrs": {"id": account_id}})
    return {"type": "paragraph", "content": nodes}


def _build_qa_fail_adf(
    reason: str | None,
    loom_url: str | None,
    image_urls: list[str] | None = None,
    mention_account_ids: list[str] | None = None,
) -> dict | None:
    """Build the ADF body for a QA→To Do fail-back comment.

    The reason is the load-bearing field — devs need to see *why* the
    ticket bounced without expanding anything, so it's rendered above the
    fold (not inside an expand node). Loom + image links sit below as
    clickable references; mentioned accountIds get a final "cc:" paragraph
    that triggers Jira notifications. Returns None if no reason is
    supplied: callers use that signal to skip posting (the transition
    still runs). Mentions without a reason still return None — there's
    no value in pinging people on an empty comment.
    """
    reason = (reason or "").strip()
    if not reason:
        return None

    loom_url = (loom_url or "").strip()
    images = _normalize_url_list(image_urls)

    content: list[dict] = [
        {"type": "paragraph", "content": [{"type": "text", "text": QA_FAIL_MARKER}]}
    ]

    reason_doc = markdown_to_adf(reason)
    content.extend(reason_doc.get("content", []))

    if loom_url:
        content.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "📹 Loom: "},
                {
                    "type": "text",
                    "text": loom_url,
                    "marks": [{"type": "link", "attrs": {"href": loom_url}}],
                },
            ],
        })

    for url in images:
        content.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "🖼️ "},
                {
                    "type": "text",
                    "text": url,
                    "marks": [{"type": "link", "attrs": {"href": url}}],
                },
            ],
        })

    mentions_para = _build_mentions_paragraph(mention_account_ids)
    if mentions_para:
        content.append(mentions_para)

    return {"type": "doc", "version": 1, "content": content}


def _wrap_body_in_expand(adf_doc: dict, marker: str = TEST_PLAN_MARKER,
                         title: str = TEST_PLAN_EXPAND_TITLE) -> dict:
    """Wrap everything after the marker paragraph in an ADF `expand` node.

    Keeps the marker paragraph as content[0] so existing comment-update detection
    (which reads content[0]'s text) continues to work.
    """
    content = adf_doc.get("content", [])
    if len(content) < 2:
        return adf_doc
    first = content[0]
    if first.get("type") != "paragraph":
        return adf_doc
    first_text = "".join(
        node.get("text", "") for node in first.get("content", [])
        if node.get("type") == "text"
    )
    if marker not in first_text:
        return adf_doc
    return {
        **adf_doc,
        "content": [
            first,
            {
                "type": "expand",
                "attrs": {"title": title},
                "content": content[1:],
            },
        ],
    }


# Directories whose files are never worth diffing (generated, tooling, agent config)
_SKIP_PATCH_DIRS = frozenset({'.agents', '.claude', '.cursor', 'scripts', 'tooling', 'node_modules'})
# File extensions that are runtime source code
_RUNTIME_EXTENSIONS = ('.tsx', '.ts', '.jsx', '.js', '.py', '.swift', '.kt')
# Substrings in a filename that indicate it's a config/tooling file, not runtime code
_CONFIG_INDICATORS = ('config.', 'eslint', 'tsconfig', 'vite.', 'webpack.', 'jest.', 'vitest.', 'rollup.')


def _extract_repo_from_url(url: str | None) -> str | None:
    """Extract 'owner/repo' from a GitHub PR URL."""
    if not url:
        return None
    match = re.match(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/\d+", url)
    if match:
        return match.group(1)
    return None


def _is_patchable_file(filename: str) -> bool:
    """Return True for runtime source files worth including diff patches for.

    Excludes config files, tooling, generated docs, and test infrastructure
    so the LLM only sees diffs that describe actual feature behaviour changes.
    """
    parts = filename.lower().split('/')
    if parts[0] in _SKIP_PATCH_DIRS:
        return False
    basename = parts[-1]
    for indicator in _CONFIG_INDICATORS:
        if indicator in basename:
            return False
    if any(basename.endswith(s) for s in ('.test.ts', '.test.tsx', '.spec.ts', '.spec.tsx', '.test.js')):
        return False
    return basename.endswith(_RUNTIME_EXTENSIONS)


# Bounce-back detection: a "bounce" is a status transition that drops the
# ticket back to a "needs more dev work" state AFTER it had already been
# pushed to a downstream review/test state. Names are matched lowercase.
_BOUNCE_BACKWARD_TARGETS: frozenset[str] = frozenset({
    "to do", "todo", "backlog", "open", "reopened", "in progress",
})
_BOUNCE_ADVANCED_TOKENS: tuple[str, ...] = (
    "qa", "uat", "test", "ready for", "verify", "verification", "release", "stage", "done",
)


def _is_advanced_status(name: str | None) -> bool:
    if not name:
        return False
    n = name.lower()
    return any(token in n for token in _BOUNCE_ADVANCED_TOKENS)


def _is_backward_target(name: str | None) -> bool:
    if not name:
        return False
    return name.strip().lower() in _BOUNCE_BACKWARD_TARGETS


def _parse_jira_timestamp(ts: str | None):
    """Parse Jira's ISO-8601 timestamps (handles trailing Z and ±HHMM offsets)."""
    if not ts:
        return None
    from datetime import datetime
    s = ts.strip()
    # Jira returns "...+0000" — datetime.fromisoformat needs "+00:00" pre-3.11.
    if len(s) >= 5 and (s[-5] in "+-") and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _find_bounce_reason(
    comments_data: list[dict],
    transition_ts: str,
    transition_author: str | None,
) -> str | None:
    """Find the comment most likely to explain a bounce-back transition.

    Heuristic: prefer comments by the same author within ±6 hours of the
    transition; otherwise the closest comment within that window. Returns
    plain text (truncated to 1000 chars) or None.
    """
    from datetime import timedelta

    target = _parse_jira_timestamp(transition_ts)
    if not target or not comments_data:
        return None

    window = timedelta(hours=6)
    best: tuple[float, dict] | None = None  # (delta_seconds, comment_data)

    for c in comments_data:
        c_ts = _parse_jira_timestamp(c.get("created"))
        if not c_ts:
            continue
        delta = c_ts - target
        if abs(delta) > window:
            continue
        author = ((c.get("author") or {}).get("displayName")
                  or (c.get("author") or {}).get("emailAddress"))
        # Same author + posted within the window: prefer those (slight bonus)
        score = abs(delta.total_seconds())
        if transition_author and author == transition_author:
            score -= 60 * 30  # 30-minute bonus for matching author
        if best is None or score < best[0]:
            best = (score, c)

    if not best:
        return None

    body_text = extract_text_from_adf(best[1].get("body", {}))
    if not body_text or not body_text.strip():
        return None
    body_text = body_text.strip()
    if len(body_text) > 1000:
        body_text = body_text[:1000] + "..."
    return body_text


def _extract_bounce_history(
    changelog_histories: list[dict],
    comments_data: list[dict],
) -> list[BounceEvent]:
    """Scan the changelog for QA/UAT → ToDo style regressions and pair each with a reason."""
    events: list[BounceEvent] = []
    saw_advanced = False

    for history in changelog_histories:
        created = history.get("created")
        author = (history.get("author") or {}).get("displayName")
        for item in history.get("items", []):
            if item.get("field") != "status":
                continue
            from_status = item.get("fromString") or ""
            to_status = item.get("toString") or ""
            if _is_advanced_status(to_status):
                saw_advanced = True
            if (
                saw_advanced
                and _is_backward_target(to_status)
                and _is_advanced_status(from_status)
            ):
                events.append(
                    BounceEvent(
                        from_status=from_status,
                        to_status=to_status,
                        timestamp=created or "",
                        author=author,
                        reason=_find_bounce_reason(comments_data, created, author),
                    )
                )
    return events


class JiraAuthError(Exception):
    """Raised when Jira returns 401 or 403."""

    def __init__(self, message: str, status_code: int, error_type: str = "invalid") -> None:
        """
        Initialize JiraAuthError.

        Args:
            message: Error message
            status_code: HTTP status code (401 or 403)
            error_type: Type of error - "invalid", "expired", "insufficient_permissions"
        """
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


class JiraNotFoundError(Exception):
    """Raised when the requested issue does not exist."""


class JiraConnectionError(Exception):
    """Raised when Jira is unreachable or times out."""


class JiraClient:
    def __init__(self) -> None:
        self.base_url = settings.jira_url.rstrip("/")
        self.email = settings.jira_username
        self.token = settings.jira_api_token

        auth_bytes = f"{self.email}:{self.token}".encode("utf-8")
        self._auth_header = base64.b64encode(auth_bytes).decode("utf-8")

    def _parse_auth_error(self, response: httpx.Response) -> tuple[str, str]:
        """
        Parse authentication error from Jira response.

        Returns:
            Tuple of (error_message, error_type)
        """
        error_msg = ""
        try:
            error_data = response.json()
            if error_data.get("errorMessages"):
                error_msg = error_data["errorMessages"][0]
        except Exception:
            pass

        # Detect if token is expired vs invalid
        if "expired" in error_msg.lower():
            return (
                "Jira API token has expired. Please generate a new token at https://id.atlassian.com/manage-profile/security/api-tokens",
                "expired"
            )
        else:
            return (
                "Jira authentication failed. Your API token or email may be invalid. Check JIRA_USERNAME and JIRA_API_TOKEN in .env",
                "invalid"
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Basic {self._auth_header}",
        }

    async def _get_development_info(
        self, issue_id: str, issue_key: str
    ) -> DevelopmentInfo | None:
        """
        Fetch development information (commits, PRs, branches) for a Jira issue.

        This uses the internal dev-status API which is unofficial and may change.
        Returns None if the endpoint is unavailable or returns errors.

        Note: This works with GitHub, Bitbucket, and other integrations.
        For Bitbucket, use applicationType='stash' (legacy naming).
        """
        commits: list[Commit] = []
        pull_requests: list[PullRequest] = []
        branches: list[str] = []

        # Try to fetch development summary first to check what's available
        summary_url = (
            f"{self.base_url}/rest/dev-status/latest/issue/summary?issueId={issue_id}"
        )

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                summary_response = await client.get(
                    summary_url, headers=self._headers()
                )

                # If summary endpoint fails or returns 404, development info may not be available
                if summary_response.status_code != 200:
                    return None

                summary_data = summary_response.json()

                # Check which application types are available from the summary
                # Common types: GitHub, github, githubenterprise, stash (Bitbucket), etc.
                application_types = []

                # Extract application types from byInstanceType across all data types
                summary_info = summary_data.get("summary", {})
                for data_type in ["repository", "pullrequest", "branch"]:
                    if data_type in summary_info:
                        by_instance = summary_info[data_type].get("byInstanceType", {})
                        for app_type in by_instance.keys():
                            if app_type not in application_types:
                                application_types.append(app_type)

                # Fetch detailed info for each application type
                for app_type in set(application_types):
                    # Fetch commits/repository info
                    repo_url = f"{self.base_url}/rest/dev-status/latest/issue/detail"
                    repo_params = {
                        "issueId": issue_id,
                        "applicationType": app_type,
                        "dataType": "repository",
                    }

                    repo_response = await client.get(
                        repo_url, headers=self._headers(), params=repo_params
                    )

                    if repo_response.status_code == 200:
                        repo_data = repo_response.json()
                        extracted_commits = self._extract_commits(repo_data)
                        extracted_branches = self._extract_branches(repo_data)
                        commits.extend(extracted_commits)
                        branches.extend(extracted_branches)

                    # Fetch pull request info
                    pr_url = f"{self.base_url}/rest/dev-status/latest/issue/detail"
                    pr_params = {
                        "issueId": issue_id,
                        "applicationType": app_type,
                        "dataType": "pullrequest",
                    }

                    pr_response = await client.get(
                        pr_url, headers=self._headers(), params=pr_params
                    )

                    if pr_response.status_code == 200:
                        pr_data = pr_response.json()
                        extracted_prs = await self._extract_pull_requests(pr_data)
                        pull_requests.extend(extracted_prs)

        except (httpx.ConnectError, httpx.TimeoutException, Exception) as e:
            # If dev-status API is unavailable, just return None
            # This is a non-critical feature, don't block the main flow
            logger.warning(f"Dev-status API error for {issue_key}: {type(e).__name__}: {e}")
            return None

        # Return None if no development info was found
        if not commits and not pull_requests and not branches:
            return None

        # Fetch repository context from the first GitHub PR (Phase 4)
        repository_context = None
        github_client = GitHubClient() if settings.github_token else None
        if github_client:
            for pr in pull_requests:
                if pr.url and "github.com" in pr.url:
                    try:
                        repository_context = await github_client.fetch_repository_context(pr.url)
                        if repository_context:
                            logger.info("Fetched repository context (README, test examples)")
                            break  # Only fetch once from the first GitHub PR
                    except Exception as e:
                        logger.warning(f"Failed to fetch repository context: {e}")

        return DevelopmentInfo(
            commits=commits,
            pull_requests=pull_requests,
            branches=branches,
            repository_context=repository_context,
        )

    def _extract_commits(self, repo_data: dict) -> list[Commit]:
        """Extract commit information from repository data."""
        commits = []
        details = repo_data.get("detail", [])

        for detail in details:
            repositories = detail.get("repositories", [])
            for repo in repositories:
                repo_commits = repo.get("commits", [])
                for commit in repo_commits:
                    commits.append(
                        Commit(
                            message=commit.get("message", ""),
                            author=commit.get("author", {}).get("name"),
                            date=commit.get("authorTimestamp"),
                            url=commit.get("url"),
                        )
                    )

        return commits

    def _extract_branches(self, repo_data: dict) -> list[str]:
        """Extract branch names from repository data."""
        branches = []
        details = repo_data.get("detail", [])

        for detail in details:
            repositories = detail.get("repositories", [])
            for repo in repositories:
                repo_branches = repo.get("branches", [])
                for branch in repo_branches:
                    branch_name = branch.get("name")
                    if branch_name:
                        branches.append(branch_name)

        return branches

    async def _extract_pull_requests(self, pr_data: dict) -> list[PullRequest]:
        """
        Extract pull request information from PR data.

        If GitHub token is configured, enriches PRs with GitHub API data.
        """
        pull_requests = []
        details = pr_data.get("detail", [])

        # Initialize GitHub client if token is available
        github_client = GitHubClient() if settings.github_token else None

        for detail in details:
            prs = detail.get("pullRequests", [])
            for pr in prs:
                pr_url = pr.get("url")

                # Create basic PR object from Jira
                jira_author = pr.get("author", {})
                pr_obj = PullRequest(
                    title=pr.get("name", ""),
                    status=pr.get("status", "UNKNOWN"),
                    url=pr_url,
                    source_branch=pr.get("source", {}).get("branch"),
                    destination_branch=pr.get("destination", {}).get("branch"),
                    repository=_extract_repo_from_url(pr_url),
                    author=jira_author.get("name") or jira_author.get("displayName") if isinstance(jira_author, dict) else None,
                )

                # Enrich with GitHub data if available
                if github_client and pr_url and "github.com" in pr_url:
                    try:
                        gh_details = await github_client.fetch_pr_details(pr_url, include_patch=True, include_comments=True)
                        if gh_details:
                            pr_obj.github_description = gh_details.description
                            if gh_details.author:
                                pr_obj.author = gh_details.author
                            pr_obj.files_changed = [
                                FileChange(
                                    filename=fc.filename,
                                    status=fc.status,
                                    additions=fc.additions,
                                    deletions=fc.deletions,
                                    changes=fc.changes,
                                    # Only keep the patch for runtime source files;
                                    # config/tooling diffs add noise without value.
                                    patch=fc.patch if (fc.patch and _is_patchable_file(fc.filename)) else None,
                                )
                                for fc in gh_details.files_changed
                            ]
                            pr_obj.total_additions = gh_details.total_additions
                            pr_obj.total_deletions = gh_details.total_deletions
                            pr_obj.comments = [
                                PRComment(
                                    author=comment.author,
                                    body=comment.body,
                                    created_at=comment.created_at,
                                    comment_type=comment.comment_type,
                                )
                                for comment in gh_details.comments
                            ]
                            logger.info(
                                f"Enriched PR {pr_obj.title} with GitHub data: "
                                f"{len(gh_details.files_changed)} files changed, {len(gh_details.comments)} comments"
                            )
                    except Exception as e:
                        logger.warning(f"Failed to enrich PR with GitHub data: {e}")

                pull_requests.append(pr_obj)

        return pull_requests

    async def _find_prs_from_text(
        self,
        description: str | None,
        comments_data: list[dict],
        existing_pr_urls: set[str],
    ) -> list[PullRequest]:
        """
        Fallback PR discovery: scan ticket description and comments for GitHub PR URLs.

        Used when the Jira dev-status API returns no linked PRs, e.g. when a developer
        manually pastes a PR link in the description or a comment instead of using the
        official Jira-GitHub integration.

        Args:
            description: Parsed plain-text ticket description
            comments_data: Raw comment objects from Jira API (ADF format bodies)
            existing_pr_urls: PR URLs already found via dev-status (to skip duplicates)

        Returns:
            List of PullRequest objects for newly-found URLs, enriched with GitHub data
            if a token is available.
        """
        GITHUB_PR_PATTERN = re.compile(
            r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+"
        )

        found_urls: set[str] = set()

        # Scan description
        if description:
            for match in GITHUB_PR_PATTERN.finditer(description):
                url = match.group(0).rstrip(".,;)>]\"'")
                found_urls.add(url)

        # Scan all comment bodies
        for comment_data in comments_data:
            body_adf = comment_data.get("body", {})
            body_text = extract_text_from_adf(body_adf)
            if body_text:
                for match in GITHUB_PR_PATTERN.finditer(body_text):
                    url = match.group(0).rstrip(".,;)>]\"'")
                    found_urls.add(url)

        new_urls = found_urls - existing_pr_urls
        if not new_urls:
            return []

        github_client = GitHubClient() if settings.github_token else None
        pull_requests = []

        for pr_url in new_urls:
            pr_obj = PullRequest(
                title=pr_url,  # fallback; overwritten below if GitHub enrichment succeeds
                status="UNKNOWN",
                url=pr_url,
                repository=_extract_repo_from_url(pr_url),
            )

            if github_client:
                try:
                    gh_details = await github_client.fetch_pr_details(
                        pr_url, include_patch=True, include_comments=True
                    )
                    if gh_details:
                        pr_obj.title = gh_details.title or pr_url
                        if gh_details.merged:
                            pr_obj.status = "MERGED"
                        elif gh_details.state == "open":
                            pr_obj.status = "OPEN"
                        else:
                            pr_obj.status = "DECLINED"
                        if gh_details.author:
                            pr_obj.author = gh_details.author
                        pr_obj.github_description = gh_details.description
                        pr_obj.files_changed = [
                            FileChange(
                                filename=fc.filename,
                                status=fc.status,
                                additions=fc.additions,
                                deletions=fc.deletions,
                                changes=fc.changes,
                                patch=fc.patch if (fc.patch and _is_patchable_file(fc.filename)) else None,
                            )
                            for fc in gh_details.files_changed
                        ]
                        pr_obj.total_additions = gh_details.total_additions
                        pr_obj.total_deletions = gh_details.total_deletions
                        pr_obj.comments = [
                            PRComment(
                                author=comment.author,
                                body=comment.body,
                                created_at=comment.created_at,
                                comment_type=comment.comment_type,
                            )
                            for comment in gh_details.comments
                        ]
                        logger.info(
                            f"Enriched text-linked PR '{pr_obj.title}': "
                            f"{len(gh_details.files_changed)} files changed"
                        )
                except Exception as e:
                    logger.warning(f"Failed to enrich text-linked PR {pr_url}: {e}")

            pull_requests.append(pr_obj)

        return pull_requests

    def _extract_figma_url(self, description: str) -> str | None:
        """
        Extract Figma URL from ticket description.

        Looks for URLs like:
        - https://www.figma.com/file/{key}/...
        - https://figma.com/design/{key}/...
        - https://www.figma.com/proto/{key}/...

        Args:
            description: Ticket description text

        Returns:
            First Figma URL found, or None
        """
        try:
            # Match Figma URLs
            pattern = r"https?://(?:www\.)?figma\.com/(file|design|proto)/[A-Za-z0-9]+[^\s]*"
            match = re.search(pattern, description)
            if match:
                figma_url = match.group(0)
                logger.info(f"Found Figma URL in description: {figma_url}")
                return figma_url
            return None
        except Exception as e:
            logger.warning(f"Error extracting Figma URL: {e}")
            return None

    def _extract_image_attachments(self, attachments_data: list) -> list[Attachment]:
        """Extract image attachments (PNG, JPG, JPEG, GIF) from Jira attachment data."""
        IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/gif"}
        MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB limit for Claude API

        image_attachments = []
        for attachment in attachments_data:
            mime_type = attachment.get("mimeType", "").lower()
            size = attachment.get("size", 0)

            # Only include images within size limit
            if mime_type in IMAGE_MIME_TYPES and size <= MAX_IMAGE_SIZE:
                image_attachments.append(
                    Attachment(
                        filename=attachment.get("filename", ""),
                        mime_type=mime_type,
                        size=size,
                        url=attachment.get("content", ""),
                        thumbnail_url=attachment.get("thumbnail"),
                    )
                )

        # Limit to first 3 images to avoid overwhelming the LLM
        return image_attachments[:3]

    def _filter_testing_comments(self, comments_data: list[dict]) -> list[JiraComment]:
        """
        Filter comments for testing-related content using smart keyword matching.

        Hybrid approach:
        1. Fetch last 15 comments (reasonable window)
        2. Prioritize formal manual test plans (comments starting with "Manual Test Plan",
           "Test Plan", or similar headings) — these are always included first
        3. Filter remaining for testing-related keywords
        4. Return up to 5 total (formal plans first, then other testing comments, then latest)

        Excludes comments created by this tool (identified by marker).

        Args:
            comments_data: List of comment objects from Jira API

        Returns:
            List of up to 5 JiraComment objects most relevant to testing
        """
        LIMIT = 5

        # Marker used to identify comments created by this tool
        BOT_MARKER = "🤖 Generated Test Plan"

        # Phrases that indicate a formal manual test plan — prioritized above all others
        FORMAL_TEST_PLAN_MARKERS = [
            'manual test plan',
            'test plan —',
            'test plan:',
            'manual testing plan',
            'test cases:',
            'test cases —',
        ]

        # Testing-related keywords to search for
        TESTING_KEYWORDS = [
            'test', 'testing', 'qa', 'quality', 'verify', 'validation', 'validate',
            'scenario', 'edge case', 'check', 'reproduce', 'steps to', 'regression',
            'acceptance criteria', 'expected behavior', 'actual behavior', 'bug',
            'defect', 'issue', 'problem', 'fails', 'passes', 'coverage'
        ]

        # Take last 15 comments (most recent)
        recent_comments = comments_data[-15:] if len(comments_data) > 15 else comments_data

        parsed_comments = []
        formal_test_plans = []
        testing_related = []

        for comment_data in recent_comments:
            # Extract author
            author_info = comment_data.get('author', {})
            author = author_info.get('displayName', author_info.get('emailAddress', 'Unknown'))

            # Extract and parse comment body from ADF format
            body_adf = comment_data.get('body', {})
            body_text = extract_text_from_adf(body_adf)

            if not body_text:
                continue

            # Skip comments created by this tool (to avoid circular references)
            if BOT_MARKER in body_text:
                continue

            # Extract timestamps
            created = comment_data.get('created', '')
            updated = comment_data.get('updated')

            # Create JiraComment object
            jira_comment = JiraComment(
                author=author,
                body=body_text,
                created=created,
                updated=updated,
                author_account_id=author_info.get('accountId'),
            )

            parsed_comments.append(jira_comment)

            body_lower = body_text.lower()

            # Check for formal test plan first (highest priority)
            if any(marker in body_lower for marker in FORMAL_TEST_PLAN_MARKERS):
                formal_test_plans.append(jira_comment)
            elif any(keyword in body_lower for keyword in TESTING_KEYWORDS):
                testing_related.append(jira_comment)

        # Build result: formal plans first, then other testing comments, then latest
        result = list(formal_test_plans)
        for c in testing_related:
            if len(result) >= LIMIT:
                break
            if c not in result:
                result.append(c)
        for c in parsed_comments:
            if len(result) >= LIMIT:
                break
            if c not in result:
                result.append(c)
        return result

    async def download_image_as_base64(self, image_url: str) -> tuple[str, str] | None:
        """
        Download an image from Jira and return it as base64-encoded string.

        Args:
            image_url: URL of the image to download

        Returns:
            Tuple of (base64_data, media_type) or None if download fails
        """
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(image_url, headers=self._headers())
                response.raise_for_status()

                # Encode image as base64
                base64_data = base64.b64encode(response.content).decode("utf-8")
                media_type = response.headers.get("content-type", "image/jpeg")

                return (base64_data, media_type)
        except Exception as e:
            logger.warning(f"Failed to download image from {image_url}: {e}")
            return None

    async def _get_parent_issue(self, issue_key: str) -> ParentIssue | None:
        """
        Fetch parent issue with description, attachments, and Figma context.

        This method fetches the full parent ticket to extract design resources
        (Figma links, image attachments) that are often stored at the parent level
        rather than on individual sub-tasks.

        Note: Does not recursively fetch the parent's parent to avoid deep nesting.

        Args:
            issue_key: The parent issue key (e.g., "PROJ-123")

        Returns:
            ParentIssue object with all resources, or None if fetch fails
        """
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}"
        params = {
            "fields": "summary,description,labels,issuetype,attachment",
            "expand": "renderedFields"
        }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url, headers=self._headers(), params=params)
                r.raise_for_status()

                data = r.json()
                fields = data.get("fields", {})

                # Extract description with error handling for malformed ADF
                description = fields.get("description")
                description_str = None
                try:
                    description_str = extract_text_from_adf(description)
                except Exception as e:
                    logger.warning(f"Failed to parse ADF description for parent {issue_key}: {e}")
                    # Try to extract raw text as fallback
                    if description and isinstance(description, dict):
                        description_str = str(description.get("text", ""))[:500]  # First 500 chars

                # Extract image attachments from parent
                parent_attachments = self._extract_image_attachments(
                    fields.get("attachment", [])
                )

                # Extract Figma URL from parent description and fetch context
                figma_context = None
                if description_str and settings.figma_token:
                    figma_url = self._extract_figma_url(description_str)
                    if figma_url:
                        try:
                            figma_client = FigmaClient()
                            figma_context = await figma_client.fetch_file_context(figma_url)
                            if figma_context:
                                logger.info(
                                    f"Fetched Figma context from parent {issue_key}: "
                                    f"{figma_context.file_name}"
                                )
                        except Exception as e:
                            logger.warning(f"Failed to fetch parent Figma context: {e}")

                return ParentIssue(
                    key=data["key"],
                    summary=fields.get("summary", ""),
                    description=description_str if description_str else None,
                    issue_type=fields.get("issuetype", {}).get("name", ""),
                    labels=fields.get("labels", []),
                    attachments=parent_attachments if parent_attachments else None,
                    figma_context=figma_context,
                )

        except httpx.HTTPStatusError as e:
            # Handle specific HTTP errors with appropriate messages
            if e.response.status_code == 404:
                logger.warning(f"Parent issue {issue_key} not found (404)")
            elif e.response.status_code == 403:
                logger.warning(f"Access denied to parent issue {issue_key} (403)")
            elif e.response.status_code == 401:
                logger.error(f"Authentication failed when fetching parent issue {issue_key} (401)")
            else:
                logger.warning(f"HTTP error {e.response.status_code} fetching parent issue {issue_key}")
            return None
        except httpx.TimeoutException:
            logger.warning(f"Timeout while fetching parent issue {issue_key}")
            return None
        except Exception as e:
            # Catch-all for unexpected errors
            logger.error(f"Unexpected error fetching parent issue {issue_key}: {type(e).__name__}: {e}")
            return None

    async def _get_linked_issues(self, issue_links_data: list[dict]) -> LinkedIssues | None:
        """
        Fetch and parse linked issues focusing on blocking relationships.

        Jira link types we care about:
        - "Blocks" / "is blocked by": Direct dependency relationships
        - "Causes" / "is caused by": Root cause relationships

        Args:
            issue_links_data: Raw issuelinks data from Jira API

        Returns:
            LinkedIssues object with categorized links, or None if no relevant links
        """
        # Early return if no links
        if not issue_links_data:
            return None
        MAX_LINKS_PER_TYPE = 5  # Limit to prevent overwhelming the LLM

        blocks_list = []
        blocked_by_list = []
        causes_list = []
        caused_by_list = []

        # Parse issue links
        for link in issue_links_data:
            link_type_data = link.get("type", {})
            link_name = link_type_data.get("name", "").lower()

            # Determine direction: inward means current issue is the target
            # outward means current issue is the source
            inward_issue = link.get("inwardIssue")
            outward_issue = link.get("outwardIssue")

            # Process "Blocks" relationships
            if "block" in link_name:
                if outward_issue:
                    # Current issue blocks the outward issue
                    blocks_list.append(self._parse_linked_issue(outward_issue, "blocks"))
                if inward_issue:
                    # Current issue is blocked by the inward issue
                    blocked_by_list.append(self._parse_linked_issue(inward_issue, "is_blocked_by"))

            # Process "Causes" relationships
            elif "cause" in link_name:
                if outward_issue:
                    # Current issue causes the outward issue
                    causes_list.append(self._parse_linked_issue(outward_issue, "causes"))
                if inward_issue:
                    # Current issue is caused by the inward issue
                    caused_by_list.append(self._parse_linked_issue(inward_issue, "is_caused_by"))

        # Limit each type to MAX_LINKS_PER_TYPE
        blocks_list = blocks_list[:MAX_LINKS_PER_TYPE]
        blocked_by_list = blocked_by_list[:MAX_LINKS_PER_TYPE]
        causes_list = causes_list[:MAX_LINKS_PER_TYPE]
        caused_by_list = caused_by_list[:MAX_LINKS_PER_TYPE]

        # Return None if no relevant links found at all
        if not any([blocks_list, blocked_by_list, causes_list, caused_by_list]):
            return None

        # Always use lists (even if empty) for consistency
        # This makes the API predictable - consumers can always iterate without None checks
        return LinkedIssues(
            blocks=blocks_list,
            blocked_by=blocked_by_list,
            causes=causes_list,
            caused_by=caused_by_list,
        )

    def _parse_linked_issue(self, issue_data: dict | None, link_type: str) -> LinkedIssue:
        """
        Parse a linked issue from Jira API data.

        Args:
            issue_data: Issue data from the issuelinks response (can be None if link is malformed)
            link_type: Type of link ("blocks", "is_blocked_by", etc.)

        Returns:
            LinkedIssue object
        """
        # Handle None or malformed issue data
        if not issue_data:
            logger.warning(f"Received None or empty issue_data for link_type: {link_type}")
            return LinkedIssue(
                key="UNKNOWN",
                summary="Unknown Issue (malformed link data)",
                description=None,
                issue_type="Unknown",
                link_type=link_type,
                status=None,
            )

        fields = issue_data.get("fields", {})

        # Extract description and truncate if too long
        description_adf = fields.get("description")
        description_str = None
        if description_adf:
            try:
                description_str = extract_text_from_adf(description_adf)
                if description_str and len(description_str) > 500:
                    description_str = description_str[:500] + "..."
            except Exception as e:
                logger.warning(f"Failed to extract description from linked issue: {e}")
                description_str = None

        return LinkedIssue(
            key=issue_data.get("key", "UNKNOWN"),
            summary=fields.get("summary", "Unknown"),
            description=description_str,
            issue_type=fields.get("issuetype", {}).get("name", "Unknown"),
            link_type=link_type,
            status=fields.get("status", {}).get("name"),
        )

    async def search_epic_children(self, epic_key: str) -> list[EpicChildSummary]:
        """Fetch child tickets under an Epic via JQL search.

        Returns a lightweight list (key, summary, issue_type, status). Capped at the
        first page (100 children); larger Epics are truncated and a warning is logged.
        """
        if not re.match(r"^[A-Z][A-Z0-9_]*-\d+$", epic_key):
            raise ValueError(f"Invalid Jira issue key: {epic_key}")

        url = f"{self.base_url}/rest/api/3/search/jql"
        payload = {
            "jql": f"parent = {epic_key} ORDER BY created ASC",
            "fields": ["summary", "issuetype", "status"],
            "maxResults": 100,
        }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    url,
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json=payload,
                )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        if r.status_code == 403:
            raise JiraAuthError(
                "Jira access forbidden. Check permissions for searching issues.",
                status_code=403,
                error_type="insufficient_permissions",
            )
        r.raise_for_status()

        data = r.json()
        issues = data.get("issues") or []
        if data.get("nextPageToken"):
            logger.warning(
                "Epic %s has more children than the page limit; only first %d returned.",
                epic_key,
                len(issues),
            )

        children: list[EpicChildSummary] = []
        for issue in issues:
            fields = issue.get("fields") or {}
            status_field = fields.get("status") or {}
            children.append(
                EpicChildSummary(
                    key=issue.get("key", ""),
                    summary=fields.get("summary") or "",
                    issue_type=(fields.get("issuetype") or {}).get("name") or "Unknown",
                    status=status_field.get("name"),
                    status_category=(status_field.get("statusCategory") or {}).get("key"),
                )
            )
        return children

    async def list_projects(self) -> list[dict]:
        """List Jira projects accessible to the configured account.

        Returns lightweight rows ({key, name, project_type, avatar_url}) sorted
        by name. Capped at the first page (100 projects).
        """
        url = f"{self.base_url}/rest/api/3/project/search"
        params = {"orderBy": "name", "maxResults": 100}

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url, headers=self._headers(), params=params)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        if r.status_code == 403:
            raise JiraAuthError(
                "Jira access forbidden. Check permissions for browsing projects.",
                status_code=403,
                error_type="insufficient_permissions",
            )
        r.raise_for_status()

        values = r.json().get("values") or []
        projects: list[dict] = []
        for proj in values:
            avatar_urls = proj.get("avatarUrls") or {}
            projects.append({
                "key": proj.get("key", ""),
                "name": proj.get("name", ""),
                "project_type": proj.get("projectTypeKey"),
                # 24x24 is the smallest avatar; falls back to any size present.
                "avatar_url": avatar_urls.get("24x24") or next(iter(avatar_urls.values()), None),
            })
        return projects

    async def list_project_statuses(self, project_key: str) -> list[dict]:
        """List unique status columns available for issues in a project.

        Jira returns statuses grouped per issue-type; we flatten and dedupe by
        status name, preserving the statusCategory key (new/indeterminate/done)
        so the UI can group them into Backlog / In Progress / Done columns.
        """
        if not re.match(r"^[A-Z][A-Z0-9_]*$", project_key):
            raise ValueError(f"Invalid Jira project key: {project_key}")

        url = f"{self.base_url}/rest/api/3/project/{project_key}/statuses"

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 404:
            raise JiraNotFoundError(f"Project not found: {project_key}")
        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        if r.status_code == 403:
            raise JiraAuthError(
                "Jira access forbidden. Check permissions for this project.",
                status_code=403,
                error_type="insufficient_permissions",
            )
        r.raise_for_status()

        seen: dict[str, dict] = {}
        for issue_type_entry in r.json() or []:
            for status in issue_type_entry.get("statuses") or []:
                name = status.get("name")
                if not name or name in seen:
                    continue
                category = (status.get("statusCategory") or {}).get("key")
                seen[name] = {"name": name, "status_category": category}
        return list(seen.values())

    async def search_project_issues(
        self, project_key: str, status_name: str
    ) -> list[EpicChildSummary]:
        """Search issues in a project filtered by status.

        Returns lightweight rows reusing EpicChildSummary. Capped at the first
        page (100 issues); larger result sets are truncated and a warning is logged.
        """
        if not re.match(r"^[A-Z][A-Z0-9_]*$", project_key):
            raise ValueError(f"Invalid Jira project key: {project_key}")
        # Status names are user-facing strings (e.g. "In Progress"); escape any
        # quotes/backslashes before interpolating into JQL.
        escaped_status = status_name.replace("\\", "\\\\").replace('"', '\\"')

        url = f"{self.base_url}/rest/api/3/search/jql"
        payload = {
            "jql": f'project = {project_key} AND status = "{escaped_status}" ORDER BY Rank ASC, created ASC',
            "fields": ["summary", "issuetype", "status"],
            "maxResults": 100,
        }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    url,
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json=payload,
                )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        if r.status_code == 403:
            raise JiraAuthError(
                "Jira access forbidden. Check permissions for searching issues.",
                status_code=403,
                error_type="insufficient_permissions",
            )
        r.raise_for_status()

        data = r.json()
        issues = data.get("issues") or []
        if data.get("nextPageToken"):
            logger.warning(
                "Project %s status %s has more issues than the page limit; only first %d returned.",
                project_key, status_name, len(issues),
            )

        results: list[EpicChildSummary] = []
        for issue in issues:
            fields = issue.get("fields") or {}
            status_field = fields.get("status") or {}
            results.append(
                EpicChildSummary(
                    key=issue.get("key", ""),
                    summary=fields.get("summary") or "",
                    issue_type=(fields.get("issuetype") or {}).get("name") or "Unknown",
                    status=status_field.get("name"),
                    status_category=(status_field.get("statusCategory") or {}).get("key"),
                )
            )
        return results

    async def get_issue(self, issue_key: str) -> JiraIssue:
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}"
        # Ask Jira for fields we need + development info if available
        params = {"fields": "summary,description,labels,issuetype,attachment,parent,issuelinks,assignee,status", "expand": "renderedFields,changelog"}

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url, headers=self._headers(), params=params)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 404:
            raise JiraNotFoundError(f"Issue not found: {issue_key}")
        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        if r.status_code == 403:
            raise JiraAuthError(
                "Jira access forbidden. Check permissions for this issue or verify your account has proper access.",
                status_code=403,
                error_type="insufficient_permissions",
            )
        r.raise_for_status()

        data = r.json()
        fields = data.get("fields", {})
        summary = fields.get("summary") or ""
        description = fields.get("description")  # Jira Cloud often returns ADF (dict)
        labels = fields.get("labels", [])
        issue_type = fields.get("issuetype", {}).get("name", "Unknown")
        assignee_field = fields.get("assignee") or {}
        assignee = assignee_field.get("displayName") or assignee_field.get("emailAddress")
        assignee_account_id = assignee_field.get("accountId")

        status_field = fields.get("status") or {}
        status_name = status_field.get("name")
        status_category = (status_field.get("statusCategory") or {}).get("key")

        # Extract all unique assignees from changelog (ordered by first appearance).
        # Track accountIds in a parallel list so the frontend can render @mentions
        # for prior assignees, not just display-name chips.
        assignee_history: list[str] = []
        assignee_history_account_ids: list[str | None] = []
        seen_assignees: set[str] = set()
        for history in data.get("changelog", {}).get("histories", []):
            for item in history.get("items", []):
                if item.get("field") == "assignee":
                    for name, acct in (
                        (item.get("fromString"), item.get("from")),
                        (item.get("toString"), item.get("to")),
                    ):
                        if name and name not in seen_assignees:
                            seen_assignees.add(name)
                            assignee_history.append(name)
                            assignee_history_account_ids.append(acct)
        # Ensure current assignee is always included
        if assignee and assignee not in seen_assignees:
            assignee_history.append(assignee)
            assignee_history_account_ids.append(assignee_account_id)

        # Extract readable text from ADF format
        description_str = extract_text_from_adf(description)

        # Analyze description quality
        analysis = analyze_description(description_str)

        # Fetch development information (commits, PRs, branches)
        # This is optional and non-blocking - if it fails, we continue without it
        issue_id = data["id"]
        development_info = await self._get_development_info(issue_id, issue_key)

        # Extract Figma URL from description and fetch design context (Phase 5)
        if description_str and settings.figma_token:
            figma_url = self._extract_figma_url(description_str)
            if figma_url:
                try:
                    figma_client = FigmaClient()
                    figma_context = await figma_client.fetch_file_context(figma_url)
                    if figma_context and development_info:
                        development_info.figma_context = figma_context
                        logger.info(f"Enriched with Figma design context: {figma_context.file_name}")
                    elif figma_context and not development_info:
                        # Create DevelopmentInfo with just Figma context
                        development_info = DevelopmentInfo(
                            commits=[],
                            pull_requests=[],
                            branches=[],
                            repository_context=None,
                            figma_context=figma_context,
                        )
                        logger.info(f"Added Figma design context: {figma_context.file_name}")
                except Exception as e:
                    logger.warning(f"Failed to fetch Figma context: {e}")

        # Extract image attachments (PNG, JPG, JPEG, GIF)
        attachments = self._extract_image_attachments(fields.get("attachment", []))

        # Fetch and filter comments for testing-related content
        comments_data: list[dict] = []  # kept for text-based PR scanning below
        filtered_comments = None
        try:
            comments_data = await self.get_comments(issue_key)
            if comments_data:
                filtered_comments = self._filter_testing_comments(comments_data)
                if filtered_comments:
                    logger.info(f"Found {len(filtered_comments)} relevant comments for {issue_key}")
        except Exception as e:
            # Non-critical - continue without comments if fetching fails
            logger.warning(f"Failed to fetch comments for {issue_key}: {e}")

        # Detect QA/UAT → ToDo bounce-backs from the changelog. Reason text is
        # pulled from the unfiltered comment list since bounce reasons may not
        # match the testing-comment keyword filter.
        bounce_history = _extract_bounce_history(
            data.get("changelog", {}).get("histories", []),
            comments_data,
        )
        if bounce_history:
            logger.info(
                f"Detected {len(bounce_history)} bounce-back event(s) for {issue_key}"
            )

        # Fallback PR discovery: scan description and comments for GitHub PR URLs.
        # Catches PRs pasted as plain links in text when Jira-GitHub integration is absent.
        existing_pr_urls = {
            pr.url
            for pr in (development_info.pull_requests if development_info else [])
            if pr.url
        }
        text_linked_prs = await self._find_prs_from_text(
            description_str, comments_data, existing_pr_urls
        )
        if text_linked_prs:
            if development_info:
                development_info.pull_requests.extend(text_linked_prs)
            else:
                development_info = DevelopmentInfo(
                    commits=[],
                    pull_requests=text_linked_prs,
                    branches=[],
                )
            logger.info(f"Found {len(text_linked_prs)} text-linked PR(s) for {issue_key}")

        # Fetch parent issue if it exists (for sub-tasks)
        # Parent tickets often contain design resources (Figma links, images) that sub-tasks lack
        parent_issue = None
        parent_data = fields.get("parent")
        if parent_data:
            parent_key = parent_data.get("key")
            # Validate parent_key is not None and not empty string
            if parent_key and isinstance(parent_key, str) and parent_key.strip():
                logger.info(f"Fetching parent issue {parent_key} for additional context")
                parent_issue = await self._get_parent_issue(parent_key)
            elif parent_key is not None:
                # Log if we received an invalid parent key
                logger.warning(f"Invalid parent key received: '{parent_key}' (type: {type(parent_key).__name__})")
                if parent_issue:
                    resources = []
                    if parent_issue.figma_context:
                        resources.append(f"Figma: {parent_issue.figma_context.file_name}")
                    if parent_issue.attachments:
                        resources.append(f"{len(parent_issue.attachments)} images")
                    if resources:
                        logger.info(f"Parent {parent_key} has: {', '.join(resources)}")

        # Fetch linked issues (blocks, blocked by, causes, caused by)
        # These provide horizontal dependency context to complement parent hierarchy
        linked_issues = None
        issue_links = fields.get("issuelinks", [])
        if issue_links:
            linked_issues = await self._get_linked_issues(issue_links)
            if linked_issues:
                link_summary = []
                if linked_issues.blocks:
                    link_summary.append(f"blocks {len(linked_issues.blocks)}")
                if linked_issues.blocked_by:
                    link_summary.append(f"blocked by {len(linked_issues.blocked_by)}")
                if linked_issues.causes:
                    link_summary.append(f"causes {len(linked_issues.causes)}")
                if linked_issues.caused_by:
                    link_summary.append(f"caused by {len(linked_issues.caused_by)}")
                if link_summary:
                    logger.info(f"{issue_key} has links: {', '.join(link_summary)}")

        return JiraIssue(
            key=data["key"],
            summary=summary,
            description=description_str if description_str else None,
            description_analysis=analysis,
            labels=labels,
            issue_type=issue_type,
            assignee=assignee,
            assignee_account_id=assignee_account_id,
            assignee_history=assignee_history if assignee_history else None,
            assignee_history_account_ids=(
                assignee_history_account_ids if assignee_history_account_ids else None
            ),
            development_info=development_info,
            attachments=attachments if attachments else None,
            comments=filtered_comments if filtered_comments else None,
            parent=parent_issue,
            linked_issues=linked_issues,
            status=status_name,
            status_category=status_category,
            bounce_history=bounce_history if bounce_history else None,
        )

    async def post_comment(self, issue_key: str, comment_text: str) -> dict:
        """
        Post a comment to a Jira issue, or update existing test plan comment if found.

        This method checks for existing test plan comments (identified by marker text)
        and updates them instead of creating duplicates when regenerating test plans.

        Args:
            issue_key: The Jira issue key (e.g., "PROJ-123")
            comment_text: Plain text comment to post

        Returns:
            dict: Response from Jira API with comment ID and metadata
                  (includes "updated": true if existing comment was updated)

        Raises:
            JiraNotFoundError: If the issue doesn't exist
            JiraAuthError: If authentication fails or permissions are insufficient
            JiraConnectionError: If Jira is unreachable
        """
        # Add unique marker to identify test plan comments
        # Using a marker that won't be visible to users but can be detected
        marker = TEST_PLAN_MARKER
        marked_text = f"{marker}\n\n{comment_text}"

        # Check if there's already a test plan comment to update
        try:
            existing_comments = await self.get_comments(issue_key)

            # Find existing test plan comment by looking for our marker
            for comment in existing_comments:
                # Extract text from ADF format
                body = comment.get("body", {})
                if body.get("type") == "doc":
                    content = body.get("content", [])
                    # Check first paragraph for marker
                    if content and len(content) > 0:
                        first_para = content[0]
                        if first_para.get("type") == "paragraph":
                            para_content = first_para.get("content", [])
                            if para_content and len(para_content) > 0:
                                text = para_content[0].get("text", "")
                                if marker in text:
                                    # Found existing test plan comment - update it
                                    comment_id = comment.get("id")
                                    logger.info(f"Updating existing test plan comment {comment_id} on {issue_key}")
                                    result = await self.update_comment(issue_key, comment_id, marked_text)
                                    result["updated"] = True
                                    return result
        except Exception as e:
            # If fetching/checking existing comments fails, fall back to creating new
            logger.warning(f"Failed to check for existing comments on {issue_key}: {e}")

        # No existing test plan comment found - create new one
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/comment"

        payload = {
            "body": _wrap_body_in_expand(markdown_to_adf(marked_text))
        }

        headers = {
            **self._headers(),
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(url, headers=headers, json=payload)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 404:
            raise JiraNotFoundError(f"Issue not found: {issue_key}")
        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        if r.status_code == 403:
            raise JiraAuthError(
                "Jira access forbidden. Check permissions for this issue or verify your account has proper access.",
                status_code=403,
                error_type="insufficient_permissions",
            )
        r.raise_for_status()

        result = r.json()
        result["updated"] = False
        return result

    async def post_qa_pass_comment(
        self,
        issue_key: str,
        loom_url: str | None,
        summary: str | None,
        environments: list[str] | None = None,
        mention_account_ids: list[str] | None = None,
        image_urls: list[str] | None = None,
    ) -> dict | None:
        """Post a QA→UAT pass comment with optional environments / Loom / summary / images.

        Returns None when no fields are populated (nothing to post). Always
        creates a new comment — these are point-in-time records of each
        QA pass, not a single living document like the test plan.
        """
        body = _build_qa_pass_adf(
            loom_url, summary, environments, mention_account_ids, image_urls
        )
        if body is None:
            return None

        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/comment"
        headers = {**self._headers(), "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(url, headers=headers, json={"body": body})
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 404:
            raise JiraNotFoundError(f"Issue not found: {issue_key}")
        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        if r.status_code == 403:
            raise JiraAuthError(
                "Jira access forbidden. Check permissions for this issue or verify your account has proper access.",
                status_code=403,
                error_type="insufficient_permissions",
            )
        r.raise_for_status()
        return r.json()

    async def post_qa_fail_comment(
        self,
        issue_key: str,
        reason: str | None,
        loom_url: str | None,
        image_urls: list[str] | None = None,
        mention_account_ids: list[str] | None = None,
    ) -> dict | None:
        """Post a QA→To Do fail-back comment.

        Returns None when no reason is supplied (nothing to post — the
        caller still runs the transition). Always creates a new comment.
        """
        body = _build_qa_fail_adf(reason, loom_url, image_urls, mention_account_ids)
        if body is None:
            return None

        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/comment"
        headers = {**self._headers(), "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(url, headers=headers, json={"body": body})
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 404:
            raise JiraNotFoundError(f"Issue not found: {issue_key}")
        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        if r.status_code == 403:
            raise JiraAuthError(
                "Jira access forbidden. Check permissions for this issue or verify your account has proper access.",
                status_code=403,
                error_type="insufficient_permissions",
            )
        r.raise_for_status()
        return r.json()

    async def get_comments(self, issue_key: str) -> list[dict]:
        """
        Fetch all comments for a Jira issue.

        Args:
            issue_key: The Jira issue key (e.g., "PROJ-123")

        Returns:
            list[dict]: List of comment objects from Jira API

        Raises:
            JiraNotFoundError: If the issue doesn't exist
            JiraAuthError: If authentication fails or permissions are insufficient
            JiraConnectionError: If Jira is unreachable
        """
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/comment"

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 404:
            raise JiraNotFoundError(f"Issue not found: {issue_key}")
        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        if r.status_code == 403:
            raise JiraAuthError(
                "Jira access forbidden. Check permissions for this issue or verify your account has proper access.",
                status_code=403,
                error_type="insufficient_permissions",
            )
        r.raise_for_status()

        response_data = r.json()
        return response_data.get("comments", [])

    async def update_comment(self, issue_key: str, comment_id: str, comment_text: str) -> dict:
        """
        Update an existing comment on a Jira issue.

        Args:
            issue_key: The Jira issue key (e.g., "PROJ-123")
            comment_id: The ID of the comment to update
            comment_text: Plain text comment to replace with

        Returns:
            dict: Response from Jira API with updated comment metadata

        Raises:
            JiraNotFoundError: If the issue or comment doesn't exist
            JiraAuthError: If authentication fails or permissions are insufficient
            JiraConnectionError: If Jira is unreachable
        """
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/comment/{comment_id}"

        payload = {
            "body": _wrap_body_in_expand(markdown_to_adf(comment_text))
        }

        headers = {
            **self._headers(),
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.put(url, headers=headers, json=payload)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 404:
            raise JiraNotFoundError(f"Comment not found: {comment_id} on issue {issue_key}")
        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        if r.status_code == 403:
            raise JiraAuthError(
                "Jira access forbidden. Check permissions for this comment or verify your account has proper access.",
                status_code=403,
                error_type="insufficient_permissions",
            )
        r.raise_for_status()

        return r.json()

    # The configured user's accountId is fixed for the lifetime of the
    # process — cache it on the class so repeated issue fetches don't
    # each pay a /myself round-trip.
    _my_account_id_cache: str | None = None

    async def get_my_account_id(self) -> str:
        """Return the accountId of the configured Jira user (via /myself)."""
        if JiraClient._my_account_id_cache:
            return JiraClient._my_account_id_cache
        url = f"{self.base_url}/rest/api/3/myself"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        r.raise_for_status()
        account_id = r.json()["accountId"]
        JiraClient._my_account_id_cache = account_id
        return account_id

    async def list_transitions(self, issue_key: str) -> list[dict]:
        """List available transitions from the issue's current status."""
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/transitions"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 404:
            raise JiraNotFoundError(f"Issue not found: {issue_key}")
        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        r.raise_for_status()
        return r.json().get("transitions", [])

    async def transition_issue(self, issue_key: str, transition_id: str) -> None:
        """Execute a workflow transition on an issue."""
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/transitions"
        headers = {**self._headers(), "Content-Type": "application/json"}
        payload = {"transition": {"id": transition_id}}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(url, headers=headers, json=payload)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 404:
            raise JiraNotFoundError(f"Issue not found: {issue_key}")
        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        if r.status_code == 403:
            raise JiraAuthError(
                "Jira access forbidden. Check permissions for transitioning this issue.",
                status_code=403,
                error_type="insufficient_permissions",
            )
        r.raise_for_status()

    async def assign_issue(self, issue_key: str, account_id: str | None) -> None:
        """Assign an issue to the given accountId. Pass None to unassign."""
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/assignee"
        headers = {**self._headers(), "Content-Type": "application/json"}
        payload = {"accountId": account_id}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.put(url, headers=headers, json=payload)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 404:
            raise JiraNotFoundError(f"Issue not found: {issue_key}")
        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        if r.status_code == 403:
            raise JiraAuthError(
                "Jira access forbidden. Check permissions for assigning this issue.",
                status_code=403,
                error_type="insufficient_permissions",
            )
        r.raise_for_status()

    async def get_prior_assignee_account_id(
        self, issue_key: str, exclude_account_id: str | None = None
    ) -> tuple[str | None, str | None]:
        """Return (accountId, displayName) of the most recent prior assignee from changelog.

        Walks the changelog newest-first; the most recent assignee change's `from`
        field is the person who had the ticket before the current assignee. Returns
        (None, None) if there's no prior assignee (e.g. ticket only ever had one
        assignee, or was just unassigned → assigned).

        `exclude_account_id` skips entries whose `from` matches it. Pass the
        bot's own account when reassigning out of testing — pull-to-testing
        always parks the ticket on the bot, so those entries aren't real
        developer ownership and shouldn't win as "prior assignee".
        """
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}"
        params = {"fields": "assignee", "expand": "changelog"}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url, headers=self._headers(), params=params)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 404:
            raise JiraNotFoundError(f"Issue not found: {issue_key}")
        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        r.raise_for_status()

        data = r.json()
        # Jira returns histories oldest-first; reverse to scan newest-first.
        # Walk all assignee changes and return the most recent non-null `from`.
        # We can't bail on the first assignee item: a "null -> me" entry (e.g.
        # picking up an unassigned ticket) hides earlier assignees behind it.
        histories = list(reversed(data.get("changelog", {}).get("histories", [])))
        for history in histories:
            for item in history.get("items", []):
                if item.get("field") != "assignee":
                    continue
                prior_id = item.get("from")
                if not prior_id:
                    continue
                if exclude_account_id and prior_id == exclude_account_id:
                    continue
                prior_name = item.get("fromString")
                if is_blocked_bot_display_name(prior_name):
                    continue
                return prior_id, prior_name
        return None, None

    async def _get_issue_internal_id(self, issue_key: str) -> str | None:
        """Fetch only the numeric internal ID for an issue key. Used by the
        dev-status API, which keys off issueId rather than issueKey.
        """
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}"
        params = {"fields": "summary"}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url, headers=self._headers(), params=params)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 404:
            raise JiraNotFoundError(f"Issue not found: {issue_key}")
        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        if r.status_code != 200:
            return None
        return r.json().get("id")

    async def _list_dev_status_pr_urls(self, issue_id: str) -> list[str]:
        """Return GitHub PR URLs linked to an issue via the dev-status API.

        Slim variant of `_get_development_info` that skips commits, branches,
        and per-PR enrichment. Used when we only need to walk linked PRs.
        """
        summary_url = (
            f"{self.base_url}/rest/dev-status/latest/issue/summary?issueId={issue_id}"
        )
        urls: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                summary_response = await client.get(summary_url, headers=self._headers())
                if summary_response.status_code != 200:
                    return []
                summary_info = summary_response.json().get("summary", {})
                pr_summary = summary_info.get("pullrequest", {}).get("byInstanceType", {})
                application_types = list(pr_summary.keys())

                for app_type in application_types:
                    pr_response = await client.get(
                        f"{self.base_url}/rest/dev-status/latest/issue/detail",
                        headers=self._headers(),
                        params={
                            "issueId": issue_id,
                            "applicationType": app_type,
                            "dataType": "pullrequest",
                        },
                    )
                    if pr_response.status_code != 200:
                        continue
                    for detail in pr_response.json().get("detail", []):
                        for pr in detail.get("pullRequests", []):
                            url = pr.get("url")
                            if url:
                                urls.append(url)
        except Exception as e:
            logger.warning(f"Dev-status PR lookup failed for issue {issue_id}: {e}")
            return []

        # De-dupe while preserving order.
        seen: set[str] = set()
        unique = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                unique.append(u)
        return unique

    async def find_user(self, query: str) -> tuple[str | None, str | None]:
        """Search Jira users by email or display name.

        Returns the first active human user (skipping app/bot accounts) as
        (accountId, displayName), or (None, None) if no match.
        """
        if not query:
            return None, None
        url = f"{self.base_url}/rest/api/3/user/search"
        params = {"query": query, "maxResults": 5}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url, headers=self._headers(), params=params)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 401:
            error_message, error_type = self._parse_auth_error(r)
            raise JiraAuthError(error_message, status_code=401, error_type=error_type)
        if r.status_code != 200:
            return None, None

        for user in r.json() or []:
            if user.get("accountType") and user.get("accountType") != "atlassian":
                continue
            if user.get("active") is False:
                continue
            return user.get("accountId"), user.get("displayName")
        return None, None

    async def get_top_pr_contributor_account_id(
        self, issue_key: str, exclude_account_id: str | None = None
    ) -> tuple[str | None, str | None]:
        """Find the Jira user who authored the linked PRs with the most code changes.

        Walks PRs surfaced by the dev-status API, scores each PR's author by
        `total_additions + total_deletions`, picks the top scorer, then maps
        the GitHub login back to a Jira accountId via (in order) commit
        author email, public profile email, public profile name, and login.

        Returns (None, None) if there are no linked PRs, the GitHub token
        isn't configured, or no Jira user matches the top contributor.

        `exclude_account_id` rejects matches resolving to that account (e.g.
        the bot itself) — name/login fallbacks are loose and could otherwise
        accidentally resolve to the bot user.
        """
        if not settings.github_token:
            return None, None

        issue_id = await self._get_issue_internal_id(issue_key)
        if not issue_id:
            return None, None

        pr_urls = await self._list_dev_status_pr_urls(issue_id)
        if not pr_urls:
            return None, None

        github_client = GitHubClient()
        # login -> {"changes": int, "pr_urls": list[str]} so we can later look
        # up a commit email from one of *this author's* PRs, not just any PR.
        author_stats: dict[str, dict] = {}
        for pr_url in pr_urls:
            if "github.com" not in pr_url:
                continue
            details = await github_client.fetch_pr_details(
                pr_url, include_patch=False, include_comments=False
            )
            if not details or not details.author:
                continue
            login = details.author
            changes = (details.total_additions or 0) + (details.total_deletions or 0)
            entry = author_stats.setdefault(login, {"changes": 0, "pr_urls": []})
            entry["changes"] += changes
            entry["pr_urls"].append(pr_url)

        if not author_stats:
            return None, None

        top_login, top_entry = max(
            author_stats.items(), key=lambda item: item[1]["changes"]
        )

        # Hand-curated mapping is the most reliable signal: GitHub display
        # names diverge from Jira (e.g. `kszombathy-skyslope` vs "Kyle
        # Szombathy") and some commit emails are GitHub noreply addresses
        # that no Jira search can resolve. Use it before falling through to
        # the loose email/name search.
        mapped = TEAM_GITHUB_LOGIN_TO_JIRA.get(top_login)
        if mapped:
            account_id, display_name = mapped
            if not exclude_account_id or account_id != exclude_account_id:
                return account_id, display_name

        # Build search queries in best-to-worst order. The first one that
        # resolves to a Jira user wins.
        queries: list[str] = []
        for pr_url in top_entry["pr_urls"]:
            commit_email = await github_client.fetch_pr_author_commit_email(pr_url)
            if commit_email:
                queries.append(commit_email)
                break

        profile = await github_client.fetch_user_profile(top_login)
        if profile:
            if profile.get("email"):
                queries.append(profile["email"])
            if profile.get("name"):
                queries.append(profile["name"])
        queries.append(top_login)

        for query in queries:
            account_id, display_name = await self.find_user(query)
            if not account_id:
                continue
            if exclude_account_id and account_id == exclude_account_id:
                continue
            if is_blocked_bot_display_name(display_name):
                continue
            return account_id, display_name
        return None, None
