import base64
import logging
import re

import httpx

from .adf_parser import extract_text_from_adf
from .config import settings
from .description_analyzer import analyze_description
from .figma_client import FigmaClient
from .github_client import GitHubClient
from .models import Attachment, Commit, DevelopmentInfo, DescriptionAnalysis, FileChange, JiraComment, JiraIssue, LinkedIssue, LinkedIssues, ParentIssue, PRComment, PullRequest, RepositoryContext

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

    def _filter_testing_comments(self, comments_data: list[dict]) -> list[JiraComment]:
        """
        Filter comments for testing-related content using smart keyword matching.

        Hybrid approach:
        1. Fetch last 10 comments (reasonable window)
        2. Filter for testing-related keywords
        3. Take top 3 matches
        4. If fewer than 3 matches, include latest comments up to 3 total

        Excludes comments created by this tool (identified by marker).

        Args:
            comments_data: List of comment objects from Jira API

        Returns:
            List of up to 3 JiraComment objects most relevant to testing
        """
        # Marker used to identify comments created by this tool
        BOT_MARKER = "🤖 Generated Test Plan"

        # Testing-related keywords to search for
        TESTING_KEYWORDS = [
            'test', 'testing', 'qa', 'quality', 'verify', 'validation', 'validate',
            'scenario', 'edge case', 'check', 'reproduce', 'steps to', 'regression',
            'acceptance criteria', 'expected behavior', 'actual behavior', 'bug',
            'defect', 'issue', 'problem', 'fails', 'passes', 'coverage'
        ]

        # Take last 10 comments (most recent)
        recent_comments = comments_data[-10:] if len(comments_data) > 10 else comments_data

        parsed_comments = []
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
                updated=updated
            )

            parsed_comments.append(jira_comment)

            # Check if comment contains testing keywords
            body_lower = body_text.lower()
            if any(keyword in body_lower for keyword in TESTING_KEYWORDS):
                testing_related.append(jira_comment)

        # Return top 3 testing-related comments, or latest 3 if fewer matches
        if len(testing_related) >= 3:
            return testing_related[:3]
        elif testing_related:
            # Have some testing comments but fewer than 3
            # Fill remaining slots with latest non-testing comments
            remaining_slots = 3 - len(testing_related)
            other_comments = [c for c in parsed_comments if c not in testing_related]
            return testing_related + other_comments[:remaining_slots]
        else:
            # No testing keywords found, return latest 3 comments
            return parsed_comments[:3]

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

    async def get_issue(self, issue_key: str) -> JiraIssue:
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}"
        # Ask Jira for fields we need + development info if available
        params = {"fields": "summary,description,labels,issuetype,attachment,parent,issuelinks", "expand": "renderedFields"}

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

        # Fetch and filter comments for testing-related content
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
            development_info=development_info,
            attachments=attachments if attachments else None,
            comments=filtered_comments if filtered_comments else None,
            parent=parent_issue,
            linked_issues=linked_issues,
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
