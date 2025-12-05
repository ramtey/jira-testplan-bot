import base64
from dataclasses import dataclass

import httpx

from .config import settings


class JiraAuthError(Exception):
    """Raised when Jira returns 401 or 403."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class JiraNotFoundError(Exception):
    """Raised when the requested issue does not exist."""


class JiraConnectionError(Exception):
    """Raised when Jira is unreachable or times out."""


@dataclass
class JiraIssue:
    key: str
    summary: str
    description: str | None


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

    async def get_issue(self, issue_key: str) -> JiraIssue:
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}"
        # Ask Jira for only what we need (faster + smaller payload)
        params = {"fields": "summary,description"}

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

        # MVP: store description as string so you can see what Jira returns
        description_str = None
        if isinstance(description, str):
            description_str = description
        elif description is not None:
            description_str = str(description)

        return JiraIssue(
            key=data["key"],
            summary=summary,
            description=description_str,
        )
