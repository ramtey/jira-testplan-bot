import base64
import logging

import httpx

from .adf_parser import extract_text_from_adf
from .config import settings
from .description_analyzer import analyze_description
from .models import Attachment, Commit, DevelopmentInfo, DescriptionAnalysis, JiraIssue, PullRequest

logger = logging.getLogger(__name__)


class JiraAuthError(Exception):
    """Raised when Jira returns 401 or 403."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class JiraNotFoundError(Exception):
    """Raised when the requested issue does not exist."""


class JiraConnectionError(Exception):
    """Raised when Jira is unreachable or times out."""


class JiraClient:
    def __init__(self) -> None:
        self.base_url = settings.jira_base_url.rstrip("/")
        self.email = settings.jira_email
        self.token = settings.jira_api_token

        auth_bytes = f"{self.email}:{self.token}".encode("utf-8")
        self._auth_header = base64.b64encode(auth_bytes).decode("utf-8")

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
                        extracted_prs = self._extract_pull_requests(pr_data)
                        pull_requests.extend(extracted_prs)

        except (httpx.ConnectError, httpx.TimeoutException, Exception) as e:
            # If dev-status API is unavailable, just return None
            # This is a non-critical feature, don't block the main flow
            logger.warning(f"Dev-status API error for {issue_key}: {type(e).__name__}: {e}")
            return None

        # Return None if no development info was found
        if not commits and not pull_requests and not branches:
            return None

        return DevelopmentInfo(
            commits=commits, pull_requests=pull_requests, branches=branches
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

    def _extract_pull_requests(self, pr_data: dict) -> list[PullRequest]:
        """Extract pull request information from PR data."""
        pull_requests = []
        details = pr_data.get("detail", [])

        for detail in details:
            prs = detail.get("pullRequests", [])
            for pr in prs:
                pull_requests.append(
                    PullRequest(
                        title=pr.get("name", ""),
                        status=pr.get("status", "UNKNOWN"),
                        url=pr.get("url"),
                        source_branch=pr.get("source", {}).get("branch"),
                        destination_branch=pr.get("destination", {}).get("branch"),
                    )
                )

        return pull_requests

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

    async def download_image_as_base64(self, image_url: str) -> tuple[str, str] | None:
        """
        Download an image from Jira and return it as base64-encoded string.

        Args:
            image_url: URL of the image to download

        Returns:
            Tuple of (base64_data, media_type) or None if download fails
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(image_url, headers=self._headers())
                response.raise_for_status()

                # Encode image as base64
                base64_data = base64.b64encode(response.content).decode("utf-8")
                media_type = response.headers.get("content-type", "image/jpeg")

                return (base64_data, media_type)
        except Exception as e:
            logger.warning(f"Failed to download image from {image_url}: {e}")
            return None

    async def get_issue(self, issue_key: str) -> JiraIssue:
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}"
        # Ask Jira for fields we need + development info if available
        params = {"fields": "summary,description,labels,issuetype,attachment", "expand": "renderedFields"}

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url, headers=self._headers(), params=params)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise JiraConnectionError(f"Failed to reach Jira: {exc}") from exc

        if r.status_code == 404:
            raise JiraNotFoundError(f"Issue not found: {issue_key}")
        if r.status_code == 401:
            raise JiraAuthError(
                "Jira authentication failed (check email/token).", status_code=401
            )
        if r.status_code == 403:
            raise JiraAuthError(
                "Jira access forbidden (check permissions for this issue).",
                status_code=403,
            )
        r.raise_for_status()

        data = r.json()
        fields = data.get("fields", {})
        summary = fields.get("summary") or ""
        description = fields.get("description")  # Jira Cloud often returns ADF (dict)
        labels = fields.get("labels", [])
        issue_type = fields.get("issuetype", {}).get("name", "Unknown")

        # Extract readable text from ADF format
        description_str = extract_text_from_adf(description)

        # Analyze description quality
        analysis = analyze_description(description_str)

        # Fetch development information (commits, PRs, branches)
        # This is optional and non-blocking - if it fails, we continue without it
        issue_id = data["id"]
        development_info = await self._get_development_info(issue_id, issue_key)

        # Extract image attachments (PNG, JPG, JPEG, GIF)
        attachments = self._extract_image_attachments(fields.get("attachment", []))

        return JiraIssue(
            key=data["key"],
            summary=summary,
            description=description_str if description_str else None,
            description_analysis=analysis,
            labels=labels,
            issue_type=issue_type,
            development_info=development_info,
            attachments=attachments if attachments else None,
        )

    async def post_comment(self, issue_key: str, comment_text: str) -> dict:
        """
        Post a comment to a Jira issue.

        Args:
            issue_key: The Jira issue key (e.g., "PROJ-123")
            comment_text: Plain text comment to post

        Returns:
            dict: Response from Jira API with comment ID and metadata

        Raises:
            JiraNotFoundError: If the issue doesn't exist
            JiraAuthError: If authentication fails or permissions are insufficient
            JiraConnectionError: If Jira is unreachable
        """
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/comment"

        # Jira Cloud uses ADF (Atlassian Document Format) for comments
        # Convert plain text to ADF format
        payload = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": comment_text
                            }
                        ]
                    }
                ]
            }
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
            raise JiraAuthError(
                "Jira authentication failed (check email/token).", status_code=401
            )
        if r.status_code == 403:
            raise JiraAuthError(
                "Jira access forbidden (check permissions for this issue).",
                status_code=403,
            )
        r.raise_for_status()

        return r.json()
