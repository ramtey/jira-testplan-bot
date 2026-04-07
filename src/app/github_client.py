"""
GitHub API client for fetching PR information and code diffs.

This module integrates with GitHub to enrich test plan context with:
- PR descriptions and titles
- Code diffs (what changed)
- File change statistics
- Modified file paths
"""

import logging
import re
from dataclasses import dataclass

import httpx

from .config import settings

logger = logging.getLogger(__name__)


class GitHubAuthError(Exception):
    """Raised when GitHub returns 401 or 403 auth-related errors."""

    def __init__(self, message: str, status_code: int, error_type: str = "invalid") -> None:
        """
        Initialize GitHubAuthError.

        Args:
            message: Error message
            status_code: HTTP status code (401 or 403)
            error_type: Type of error - "invalid", "expired", "insufficient_permissions", "rate_limited"
        """
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


@dataclass
class FileChange:
    """Represents a file change in a pull request."""

    filename: str
    status: str  # "added", "modified", "removed", "renamed"
    additions: int
    deletions: int
    changes: int
    patch: str | None = None  # The actual diff patch (optional, can be large)


@dataclass
class PRComment:
    """Represents a comment on a pull request."""

    author: str
    body: str
    created_at: str
    comment_type: str  # "conversation" or "review_comment"


@dataclass
class PRDetails:
    """Detailed PR information from GitHub."""

    number: int
    title: str
    description: str | None
    state: str  # "open", "closed", "merged"
    merged: bool
    files_changed: list[FileChange]
    total_additions: int
    total_deletions: int
    total_changes: int
    comments: list[PRComment]
    author: str | None = None  # GitHub login of the PR author


@dataclass
class RepositoryContext:
    """Repository documentation and context for test plan generation."""

    readme_content: str | None = None
    test_examples: list[str] | None = None  # Paths to example test files
    testid_reference: str | None = None     # Auto-generated testID map (from .agents/skills/simulator-testing/references/testid-reference.md)
    screen_guide: str | None = None         # Screen navigation guide (from .agents/skills/simulator-testing/references/screen-guide.md)


class GitHubClient:
    """Client for interacting with GitHub API."""

    def __init__(self, token: str | None = None):
        """
        Initialize GitHub client.

        Args:
            token: GitHub personal access token (optional)
        """
        self.token = token or settings.github_token
        self.base_url = "https://api.github.com"

    def _headers(self) -> dict:
        """Build headers for GitHub API requests."""
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "jira-testplan-bot",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _parse_auth_error(self, response: httpx.Response) -> tuple[str, str]:
        """
        Parse authentication error from GitHub response.

        Returns:
            Tuple of (error_message, error_type)
        """
        error_msg = ""
        try:
            error_data = response.json()
            error_msg = error_data.get("message", "")
        except Exception:
            pass

        # Detect specific error types
        if response.status_code == 401:
            if "Bad credentials" in error_msg:
                return (
                    "GitHub token is invalid. Please generate a new Personal Access Token at https://github.com/settings/tokens",
                    "invalid"
                )
            elif "token" in error_msg.lower() and "expired" in error_msg.lower():
                return (
                    "GitHub token has expired. Please generate a new Personal Access Token at https://github.com/settings/tokens",
                    "expired"
                )
            else:
                return (
                    "GitHub authentication failed. Check your GITHUB_TOKEN in .env",
                    "invalid"
                )
        elif response.status_code == 403:
            if "rate limit" in error_msg.lower():
                return (
                    "GitHub API rate limit exceeded. Wait and try again later.",
                    "rate_limited"
                )
            else:
                return (
                    "GitHub token lacks required permissions. Ensure 'repo' scope is enabled at https://github.com/settings/tokens",
                    "insufficient_permissions"
                )
        else:
            return (f"GitHub API error: {error_msg}", "invalid")

    def _parse_blob_url(self, url: str) -> tuple[str, str, str, str, int | None, int | None] | None:
        """
        Parse a GitHub blob URL into (owner, repo, ref, path, start_line, end_line).

        Handles URLs like:
          https://github.com/owner/repo/blob/main/src/foo.py
          https://github.com/owner/repo/blob/abc1234/src/foo.py#L42-L50
        """
        pattern = r"github\.com/([^/]+)/([^/]+)/blob/([^/]+)/([^#\s>\"'\)]+)(?:#L(\d+)(?:-L(\d+))?)?"
        match = re.search(pattern, url)
        if not match:
            return None
        owner, repo, ref, path = match.group(1), match.group(2), match.group(3), match.group(4)
        path = path.rstrip(".,;)>]\"'")
        start_line = int(match.group(5)) if match.group(5) else None
        end_line = int(match.group(6)) if match.group(6) else None
        return owner, repo, ref, path, start_line, end_line

    def _parse_commit_url(self, url: str) -> tuple[str, str, str] | None:
        """Parse a GitHub commit URL into (owner, repo, sha)."""
        pattern = r"github\.com/([^/]+)/([^/]+)/commit/([0-9a-f]{7,40})"
        match = re.search(pattern, url)
        if not match:
            return None
        return match.group(1), match.group(2), match.group(3)

    async def fetch_file_from_blob_url(self, url: str) -> dict | None:
        """
        Fetch file content from a GitHub blob URL.

        If the URL includes a line anchor (#L42 or #L42-L50), returns only that
        region with surrounding context. Otherwise returns the full file capped
        at 200 lines.

        Returns a dict with keys: path, ref, content, and optionally lines.
        """
        parsed = self._parse_blob_url(url)
        if not parsed:
            return None
        owner, repo, ref, path, start_line, end_line = parsed

        import base64
        api_url = f"{self.base_url}/repos/{owner}/{repo}/contents/{path}?ref={ref}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(api_url, headers=self._headers())
                if response.status_code != 200:
                    logger.warning(f"GitHub file fetch returned {response.status_code} for {url}")
                    return None
                data = response.json()
                content = base64.b64decode(data.get("content", "")).decode("utf-8")
        except Exception as e:
            logger.warning(f"Failed to fetch file from blob URL {url}: {e}")
            return None

        lines = content.splitlines()

        if start_line:
            ctx = 10
            lo = max(0, start_line - 1 - ctx)
            hi = min(len(lines), (end_line or start_line) + ctx)
            snippet = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines[lo:hi], start=lo))
            label = f"L{start_line}" + (f"-L{end_line}" if end_line else "")
            return {"path": path, "ref": ref, "content": snippet, "lines": label}

        MAX_LINES = 200
        if len(lines) > MAX_LINES:
            content = "\n".join(lines[:MAX_LINES]) + f"\n... (truncated — {len(lines)} total lines)"
        return {"path": path, "ref": ref, "content": content}

    async def fetch_commit_from_url(self, url: str) -> dict | None:
        """
        Fetch a commit's metadata and diff from a GitHub commit URL.

        Returns a dict with keys: sha, message, files (list of changed files with patches).
        Patches are capped at 2 KB each and at most 10 files are included.
        """
        parsed = self._parse_commit_url(url)
        if not parsed:
            return None
        owner, repo, sha = parsed

        api_url = f"{self.base_url}/repos/{owner}/{repo}/commits/{sha}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(api_url, headers=self._headers())
                if response.status_code != 200:
                    logger.warning(f"GitHub commit fetch returned {response.status_code} for {url}")
                    return None
                data = response.json()
        except Exception as e:
            logger.warning(f"Failed to fetch commit from URL {url}: {e}")
            return None

        commit_msg = data.get("commit", {}).get("message", "").split("\n")[0]
        files = [
            {
                "filename": f["filename"],
                "status": f["status"],
                "additions": f["additions"],
                "deletions": f["deletions"],
                "patch": f.get("patch", "")[:2000],
            }
            for f in data.get("files", [])[:10]
        ]
        return {"sha": sha[:8], "message": commit_msg, "files": files}

    def _parse_github_url(self, pr_url: str) -> tuple[str, str, int] | None:
        """
        Parse GitHub PR URL to extract owner, repo, and PR number.

        Args:
            pr_url: GitHub PR URL (e.g., https://github.com/owner/repo/pull/123)

        Returns:
            Tuple of (owner, repo, pr_number) or None if parsing fails
        """
        # Match: https://github.com/owner/repo/pull/123
        pattern = r"github\.com/([^/]+)/([^/]+)/pull/(\d+)"
        match = re.search(pattern, pr_url)

        if match:
            owner, repo, pr_number = match.groups()
            return (owner, repo, int(pr_number))

        logger.warning(f"Failed to parse GitHub PR URL: {pr_url}")
        return None

    async def _fetch_pr_comments(self, client: httpx.AsyncClient, owner: str, repo: str, pr_number: int) -> list[PRComment]:
        """
        Fetch all comments for a PR (conversation + review comments).

        Args:
            client: HTTP client to use
            owner: Repository owner
            repo: Repository name
            pr_number: PR number

        Returns:
            List of PRComment objects
        """
        comments = []

        try:
            # Fetch conversation comments (uses issues API since PRs are issues)
            issues_comments_url = f"{self.base_url}/repos/{owner}/{repo}/issues/{pr_number}/comments"
            conversation_response = await client.get(issues_comments_url, headers=self._headers())

            if conversation_response.status_code == 200:
                conversation_data = conversation_response.json()
                for comment in conversation_data:
                    comments.append(
                        PRComment(
                            author=comment.get("user", {}).get("login", "unknown"),
                            body=comment.get("body", ""),
                            created_at=comment.get("created_at", ""),
                            comment_type="conversation",
                        )
                    )

            # Fetch review comments (line-specific comments)
            review_comments_url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pr_number}/comments"
            review_response = await client.get(review_comments_url, headers=self._headers())

            if review_response.status_code == 200:
                review_data = review_response.json()
                for comment in review_data:
                    # Include file path context for review comments
                    body = comment.get("body", "")
                    file_path = comment.get("path", "")
                    if file_path:
                        body = f"[{file_path}] {body}"

                    comments.append(
                        PRComment(
                            author=comment.get("user", {}).get("login", "unknown"),
                            body=body,
                            created_at=comment.get("created_at", ""),
                            comment_type="review_comment",
                        )
                    )

            logger.info(f"Fetched {len(comments)} comments for PR #{pr_number}")
            return comments

        except Exception as e:
            logger.warning(f"Failed to fetch PR comments: {e}")
            return []

    async def fetch_pr_details(self, pr_url: str, include_patch: bool = False, include_comments: bool = True) -> PRDetails | None:
        """
        Fetch detailed PR information from GitHub.

        Args:
            pr_url: GitHub PR URL
            include_patch: Whether to include the actual diff patch (can be large)
            include_comments: Whether to fetch PR comments (conversation + review comments)

        Returns:
            PRDetails object or None if fetch fails
        """
        if not self.token:
            logger.warning("GitHub token not configured - skipping PR details fetch")
            return None

        parsed = self._parse_github_url(pr_url)
        if not parsed:
            return None

        owner, repo, pr_number = parsed

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Fetch PR details
                pr_url_api = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pr_number}"
                pr_response = await client.get(pr_url_api, headers=self._headers())
                pr_response.raise_for_status()
                pr_data = pr_response.json()

                # Fetch PR files (changes)
                files_url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pr_number}/files"
                files_response = await client.get(files_url, headers=self._headers())
                files_response.raise_for_status()
                files_data = files_response.json()

                # Parse file changes
                file_changes = []
                total_additions = 0
                total_deletions = 0

                for file in files_data:
                    additions = file.get("additions", 0)
                    deletions = file.get("deletions", 0)
                    changes = file.get("changes", 0)

                    total_additions += additions
                    total_deletions += deletions

                    file_changes.append(
                        FileChange(
                            filename=file.get("filename", ""),
                            status=file.get("status", "unknown"),
                            additions=additions,
                            deletions=deletions,
                            changes=changes,
                            patch=file.get("patch") if include_patch else None,
                        )
                    )

                # Fetch PR comments if requested
                comments = []
                if include_comments:
                    comments = await self._fetch_pr_comments(client, owner, repo, pr_number)

                return PRDetails(
                    number=pr_data.get("number"),
                    title=pr_data.get("title", ""),
                    description=pr_data.get("body"),
                    state=pr_data.get("state", "unknown"),
                    merged=pr_data.get("merged", False),
                    files_changed=file_changes,
                    total_additions=total_additions,
                    total_deletions=total_deletions,
                    total_changes=total_additions + total_deletions,
                    comments=comments,
                    author=pr_data.get("user", {}).get("login"),
                )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning(f"PR not found or no access: {pr_url}")
            elif e.response.status_code == 403:
                logger.warning(f"GitHub API rate limit or insufficient permissions: {pr_url}")
            else:
                logger.error(f"Failed to fetch PR details from {pr_url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching PR from {pr_url}: {e}")
            return None

    def format_pr_summary(self, pr_details: PRDetails) -> str:
        """
        Format PR details as a human-readable summary for LLM context.

        Args:
            pr_details: PR details from GitHub

        Returns:
            Formatted summary string
        """
        summary = f"**PR #{pr_details.number}: {pr_details.title}**\n"
        summary += f"Status: {pr_details.state}"
        if pr_details.merged:
            summary += " (merged)"
        summary += "\n\n"

        if pr_details.description:
            # Truncate long descriptions
            desc = pr_details.description[:500]
            if len(pr_details.description) > 500:
                desc += "..."
            summary += f"Description: {desc}\n\n"

        # File changes summary
        summary += f"**Code Changes ({len(pr_details.files_changed)} files changed):**\n"
        summary += f"- Total: +{pr_details.total_additions} additions, -{pr_details.total_deletions} deletions\n\n"

        # Group files by type/directory
        summary += "**Modified Files:**\n"
        for file_change in pr_details.files_changed[:20]:  # Limit to first 20 files
            status_icon = {
                "added": "✨",
                "modified": "📝",
                "removed": "🗑️",
                "renamed": "📛",
            }.get(file_change.status, "📄")

            summary += f"{status_icon} `{file_change.filename}` (+{file_change.additions}/-{file_change.deletions})\n"

        if len(pr_details.files_changed) > 20:
            summary += f"... and {len(pr_details.files_changed) - 20} more files\n"

        return summary

    async def fetch_repository_context(self, pr_url: str) -> RepositoryContext | None:
        """
        Fetch repository documentation and context from GitHub.

        Args:
            pr_url: GitHub PR URL (used to identify the repository)

        Returns:
            RepositoryContext object or None if fetch fails
        """
        if not self.token:
            logger.warning("GitHub token not configured - skipping repository context fetch")
            return None

        parsed = self._parse_github_url(pr_url)
        if not parsed:
            return None

        owner, repo, _ = parsed

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Fetch README.md
                readme_content = await self._fetch_file_content(client, owner, repo, "README.md")

                # Find test file examples
                test_examples = await self._find_test_files(client, owner, repo)

                # Fetch simulator/UI testing context if present in this repo.
                # These files are created by the simulator-testing skill pattern.
                # Returns None silently for repos that don't use this convention.
                testid_reference = await self._fetch_file_content(
                    client, owner, repo,
                    ".agents/skills/simulator-testing/references/testid-reference.md",
                )
                screen_guide = await self._fetch_file_content(
                    client, owner, repo,
                    ".agents/skills/simulator-testing/references/screen-guide.md",
                )

                if testid_reference:
                    logger.info(f"Fetched testID reference from {owner}/{repo}")
                if screen_guide:
                    logger.info(f"Fetched screen guide from {owner}/{repo}")

                return RepositoryContext(
                    readme_content=readme_content,
                    test_examples=test_examples,
                    testid_reference=testid_reference,
                    screen_guide=screen_guide,
                )

        except Exception as e:
            logger.warning(f"Failed to fetch repository context: {e}")
            return None

    async def _fetch_file_content(self, client: httpx.AsyncClient, owner: str, repo: str, file_path: str) -> str | None:
        """
        Fetch content of a specific file from the repository.

        Args:
            client: HTTP client to use
            owner: Repository owner
            repo: Repository name
            file_path: Path to the file in the repository

        Returns:
            File content as string or None if not found
        """
        try:
            # Try main branch first
            for branch in ["main", "master"]:
                url = f"{self.base_url}/repos/{owner}/{repo}/contents/{file_path}?ref={branch}"
                response = await client.get(url, headers=self._headers())

                if response.status_code == 200:
                    data = response.json()
                    # GitHub returns base64-encoded content
                    import base64
                    content = base64.b64decode(data.get("content", "")).decode("utf-8")
                    logger.info(f"Fetched {file_path} from {owner}/{repo}")
                    return content

            logger.info(f"File {file_path} not found in {owner}/{repo}")
            return None

        except Exception as e:
            logger.warning(f"Failed to fetch {file_path}: {e}")
            return None

    async def _find_test_files(self, client: httpx.AsyncClient, owner: str, repo: str) -> list[str]:
        """
        Find test files in the repository to learn testing patterns.

        Args:
            client: HTTP client to use
            owner: Repository owner
            repo: Repository name

        Returns:
            List of test file paths (limited to 5 examples)
        """
        test_patterns = [
            "__tests__",
            "tests",
            "test",
            "spec",
        ]

        test_files = []

        try:
            # Search for test files using GitHub search API
            # Limit to common test file extensions
            query = f"repo:{owner}/{repo} extension:test OR extension:spec OR path:tests OR path:__tests__"
            search_url = f"{self.base_url}/search/code?q={query}&per_page=5"

            response = await client.get(search_url, headers=self._headers())

            if response.status_code == 200:
                data = response.json()
                items = data.get("items", [])

                for item in items[:5]:  # Limit to 5 examples
                    test_files.append(item.get("path", ""))

                if test_files:
                    logger.info(f"Found {len(test_files)} test file examples in {owner}/{repo}")

        except Exception as e:
            logger.warning(f"Failed to find test files: {e}")

        return test_files
