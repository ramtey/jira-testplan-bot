import base64
import logging
import re

import httpx

from .adf_parser import extract_text_from_adf
from .config import settings
from .description_analyzer import analyze_description
from .figma_client import FigmaClient
from .github_client import GitHubClient
from .models import Attachment, Commit, DevelopmentInfo, DescriptionAnalysis, FileChange, JiraIssue, PRComment, PullRequest, RepositoryContext

logger = logging.getLogger(__name__)


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
        self.base_url = settings.jira_base_url.rstrip("/")
        self.email = settings.jira_email
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
                "Jira authentication failed. Your API token or email may be invalid. Check JIRA_EMAIL and JIRA_API_TOKEN in .env",
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
                pr_obj = PullRequest(
                    title=pr.get("name", ""),
                    status=pr.get("status", "UNKNOWN"),
                    url=pr_url,
                    source_branch=pr.get("source", {}).get("branch"),
                    destination_branch=pr.get("destination", {}).get("branch"),
                )

                # Enrich with GitHub data if available
                if github_client and pr_url and "github.com" in pr_url:
                    try:
                        gh_details = await github_client.fetch_pr_details(pr_url, include_patch=False, include_comments=True)
                        if gh_details:
                            pr_obj.github_description = gh_details.description
                            pr_obj.files_changed = [
                                FileChange(
                                    filename=fc.filename,
                                    status=fc.status,
                                    additions=fc.additions,
                                    deletions=fc.deletions,
                                    changes=fc.changes,
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
        marker = "🤖 Generated Test Plan"
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
                                "text": marked_text
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
