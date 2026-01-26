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
