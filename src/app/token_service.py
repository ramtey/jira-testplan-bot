"""
Token Health Check Service.

Centralized service for validating API tokens across all integrations:
- Jira API Token
- GitHub Personal Access Token
- Anthropic/Claude API Key

Provides detailed status information including:
- Token validity (valid/invalid/expired/missing)
- Error messages and remediation steps
- Service availability and rate limits
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import httpx

from .config import settings
from .model_capabilities import TOKEN_VALIDATION_MODEL

logger = logging.getLogger(__name__)


#: A syntactically valid Figma file key that cannot exist. Reading it proves
#: the token authenticates and carries file scope without spending quota:
#: Figma charges /v1/files by what it returns, and a miss returns nothing.
_FIGMA_PROBE_MISS_KEY = "000000000000000000000000"

#: Below this, a Figma 429 is a burst throttle that clears itself and is not
#: worth waking anyone over. Above it, the file-read quota is spent — which on
#: 2026-09-16 meant a Retry-After of 241725s (2.8 days) with design context
#: silently missing from every plan the whole time.
_FIGMA_THROTTLE_TOLERANCE_S = 3600


def _retry_after_seconds(response) -> int | None:
    """Figma's Retry-After in seconds, or None when it did not say."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _humanize_seconds(seconds: int | None) -> str:
    if not seconds or seconds < 0:
        return "an unknown time"
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86400:.1f} days"


class TokenErrorType(str, Enum):
    """Types of token errors."""
    VALID = "valid"
    MISSING = "missing"
    INVALID = "invalid"
    EXPIRED = "expired"
    RATE_LIMITED = "rate_limited"
    INSUFFICIENT_PERMISSIONS = "insufficient_permissions"
    SERVICE_UNAVAILABLE = "service_unavailable"


@dataclass
class TokenStatus:
    """Status information for an API token."""
    service_name: str
    is_valid: bool
    is_required: bool
    error_type: TokenErrorType | None = None
    error_message: str | None = None
    help_url: str | None = None
    last_checked: datetime | None = None
    details: dict | None = None


class TokenHealthService:
    """Service for checking health of all API tokens."""

    def __init__(self):
        self.timeout = 10.0  # seconds

    async def validate_jira_token(self) -> TokenStatus:
        """
        Validate Jira API token by making a test API call.

        Returns:
            TokenStatus with validation result
        """
        service_name = "Jira"
        last_checked = datetime.now()

        # Check if token is configured
        if not settings.jira_api_token or not settings.jira_username or not settings.jira_url:
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=True,
                error_type=TokenErrorType.MISSING,
                error_message="Jira credentials not configured. Please set JIRA_URL, JIRA_USERNAME, and JIRA_API_TOKEN in .env",
                help_url="https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/",
                last_checked=last_checked,
            )

        try:
            # Test API call - get current user (lightweight endpoint)
            import base64
            auth_bytes = f"{settings.jira_username}:{settings.jira_api_token}".encode("utf-8")
            auth_header = base64.b64encode(auth_bytes).decode("utf-8")

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{settings.jira_url.rstrip('/')}/rest/api/2/myself",
                    headers={
                        "Authorization": f"Basic {auth_header}",
                        "Content-Type": "application/json",
                    },
                )

                if response.status_code == 200:
                    user_data = response.json()
                    return TokenStatus(
                        service_name=service_name,
                        is_valid=True,
                        is_required=True,
                        error_type=TokenErrorType.VALID,
                        last_checked=last_checked,
                        details={
                            "user_email": user_data.get("emailAddress"),
                            "user_name": user_data.get("displayName"),
                        },
                    )
                elif response.status_code == 401:
                    error_data = response.json() if response.text else {}
                    error_msg = error_data.get("errorMessages", [""])[0] if error_data.get("errorMessages") else ""

                    # Try to detect if token is expired vs invalid
                    if "expired" in error_msg.lower():
                        return TokenStatus(
                            service_name=service_name,
                            is_valid=False,
                            is_required=True,
                            error_type=TokenErrorType.EXPIRED,
                            error_message="Jira API token has expired. Please generate a new token.",
                            help_url="https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/",
                            last_checked=last_checked,
                        )
                    else:
                        return TokenStatus(
                            service_name=service_name,
                            is_valid=False,
                            is_required=True,
                            error_type=TokenErrorType.INVALID,
                            error_message="Jira authentication failed. Check your email and API token.",
                            help_url="https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/",
                            last_checked=last_checked,
                            details={"status_code": 401, "error": error_msg},
                        )
                elif response.status_code == 403:
                    return TokenStatus(
                        service_name=service_name,
                        is_valid=False,
                        is_required=True,
                        error_type=TokenErrorType.INSUFFICIENT_PERMISSIONS,
                        error_message="Jira API token lacks required permissions.",
                        help_url="https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/",
                        last_checked=last_checked,
                        details={"status_code": 403},
                    )
                else:
                    return TokenStatus(
                        service_name=service_name,
                        is_valid=False,
                        is_required=True,
                        error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                        error_message=f"Jira API returned unexpected status: {response.status_code}",
                        last_checked=last_checked,
                        details={"status_code": response.status_code},
                    )

        except httpx.ConnectError as e:
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=True,
                error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                error_message=f"Cannot connect to Jira at {settings.jira_url}. Check URL and network.",
                last_checked=last_checked,
                details={"error": str(e)},
            )
        except httpx.TimeoutException:
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=True,
                error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                error_message=f"Jira connection timed out after {self.timeout}s.",
                last_checked=last_checked,
            )
        except Exception as e:
            logger.error(f"Unexpected error validating Jira token: {e}")
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=True,
                error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                error_message=f"Unexpected error: {str(e)}",
                last_checked=last_checked,
            )

    async def validate_github_token(self) -> TokenStatus:
        """
        Validate GitHub personal access token.

        Returns:
            TokenStatus with validation result
        """
        service_name = "GitHub"
        last_checked = datetime.now()

        # GitHub is optional
        if not settings.github_token:
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=False,
                error_type=TokenErrorType.MISSING,
                error_message="GitHub token not configured (optional). Set GITHUB_TOKEN for enhanced features.",
                help_url="https://github.com/settings/tokens",
                last_checked=last_checked,
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    "https://api.github.com/user",
                    headers={
                        "Authorization": f"Bearer {settings.github_token}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                )

                if response.status_code == 200:
                    user_data = response.json()
                    return TokenStatus(
                        service_name=service_name,
                        is_valid=True,
                        is_required=False,
                        error_type=TokenErrorType.VALID,
                        last_checked=last_checked,
                        details={
                            "user_login": user_data.get("login"),
                            "user_name": user_data.get("name"),
                        },
                    )
                elif response.status_code == 401:
                    error_data = response.json() if response.text else {}
                    error_msg = error_data.get("message", "")

                    # GitHub specific error messages
                    if "Bad credentials" in error_msg:
                        return TokenStatus(
                            service_name=service_name,
                            is_valid=False,
                            is_required=False,
                            error_type=TokenErrorType.INVALID,
                            error_message="GitHub token is invalid. Please generate a new token.",
                            help_url="https://github.com/settings/tokens",
                            last_checked=last_checked,
                        )
                    elif "token" in error_msg.lower() and "expired" in error_msg.lower():
                        return TokenStatus(
                            service_name=service_name,
                            is_valid=False,
                            is_required=False,
                            error_type=TokenErrorType.EXPIRED,
                            error_message="GitHub token has expired. Please generate a new token.",
                            help_url="https://github.com/settings/tokens",
                            last_checked=last_checked,
                        )
                    else:
                        return TokenStatus(
                            service_name=service_name,
                            is_valid=False,
                            is_required=False,
                            error_type=TokenErrorType.INVALID,
                            error_message="GitHub authentication failed. Check your token.",
                            help_url="https://github.com/settings/tokens",
                            last_checked=last_checked,
                        )
                elif response.status_code == 403:
                    error_data = response.json() if response.text else {}
                    error_msg = error_data.get("message", "")

                    # Check for rate limiting
                    if "rate limit" in error_msg.lower():
                        return TokenStatus(
                            service_name=service_name,
                            is_valid=True,  # Token is valid, just rate limited
                            is_required=False,
                            error_type=TokenErrorType.RATE_LIMITED,
                            error_message="GitHub API rate limit exceeded. Wait and try again.",
                            last_checked=last_checked,
                            details={
                                "rate_limit_reset": response.headers.get("X-RateLimit-Reset"),
                            },
                        )
                    else:
                        return TokenStatus(
                            service_name=service_name,
                            is_valid=False,
                            is_required=False,
                            error_type=TokenErrorType.INSUFFICIENT_PERMISSIONS,
                            error_message="GitHub token lacks required permissions. Ensure 'repo' scope is enabled.",
                            help_url="https://github.com/settings/tokens",
                            last_checked=last_checked,
                        )
                else:
                    return TokenStatus(
                        service_name=service_name,
                        is_valid=False,
                        is_required=False,
                        error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                        error_message=f"GitHub API returned unexpected status: {response.status_code}",
                        last_checked=last_checked,
                    )

        except httpx.ConnectError as e:
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=False,
                error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                error_message="Cannot connect to GitHub API. Check network.",
                last_checked=last_checked,
                details={"error": str(e)},
            )
        except httpx.TimeoutException:
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=False,
                error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                error_message=f"GitHub connection timed out after {self.timeout}s.",
                last_checked=last_checked,
            )
        except Exception as e:
            logger.error(f"Unexpected error validating GitHub token: {e}")
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=False,
                error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                error_message=f"Unexpected error: {str(e)}",
                last_checked=last_checked,
            )

    async def validate_anthropic_token(self) -> TokenStatus:
        """
        Validate Anthropic/Claude API key.

        Returns:
            TokenStatus with validation result
        """
        service_name = "Claude (Anthropic)"
        last_checked = datetime.now()

        # Check if using Claude provider
        if settings.llm_provider.lower() != "claude":
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=False,
                error_type=TokenErrorType.MISSING,
                error_message=f"Not using Claude provider (current: {settings.llm_provider}). No validation needed.",
                last_checked=last_checked,
            )

        if not settings.anthropic_api_key:
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=True,
                error_type=TokenErrorType.MISSING,
                error_message="Anthropic API key not configured. Set ANTHROPIC_API_KEY in .env",
                help_url="https://console.anthropic.com/settings/keys",
                last_checked=last_checked,
            )

        try:
            # Make a minimal API call to validate the key
            # Using a very small prompt to minimize cost
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "anthropic-version": "2023-06-01",
                        "x-api-key": settings.anthropic_api_key,
                        "content-type": "application/json",
                    },
                    json={
                        "model": TOKEN_VALIDATION_MODEL,
                        "max_tokens": 10,
                        "messages": [{"role": "user", "content": "Hi"}],
                    },
                )

                if response.status_code == 200:
                    return TokenStatus(
                        service_name=service_name,
                        is_valid=True,
                        is_required=True,
                        error_type=TokenErrorType.VALID,
                        last_checked=last_checked,
                        details={
                            "model": settings.llm_model,
                        },
                    )
                elif response.status_code == 401:
                    error_data = response.json() if response.text else {}
                    error_msg = error_data.get("error", {}).get("message", "")

                    if "invalid" in error_msg.lower():
                        return TokenStatus(
                            service_name=service_name,
                            is_valid=False,
                            is_required=True,
                            error_type=TokenErrorType.INVALID,
                            error_message="Anthropic API key is invalid. Check your key.",
                            help_url="https://console.anthropic.com/settings/keys",
                            last_checked=last_checked,
                        )
                    else:
                        return TokenStatus(
                            service_name=service_name,
                            is_valid=False,
                            is_required=True,
                            error_type=TokenErrorType.EXPIRED,
                            error_message="Anthropic API authentication failed. Key may be expired or revoked.",
                            help_url="https://console.anthropic.com/settings/keys",
                            last_checked=last_checked,
                        )
                elif response.status_code == 429:
                    return TokenStatus(
                        service_name=service_name,
                        is_valid=True,  # Token is valid, just rate limited
                        is_required=True,
                        error_type=TokenErrorType.RATE_LIMITED,
                        error_message="Anthropic API rate limit exceeded. Wait and try again.",
                        last_checked=last_checked,
                    )
                elif response.status_code == 529:
                    # "Overloaded" — a transient state on Anthropic's side, not
                    # a bad key. Falling through to the generic branch reported
                    # is_valid=False, so the token banner cried wolf about
                    # ANTHROPIC_API_KEY during what was a few-second blip.
                    return TokenStatus(
                        service_name=service_name,
                        is_valid=True,
                        is_required=True,
                        error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                        error_message="Claude is temporarily overloaded. Try again in a moment.",
                        last_checked=last_checked,
                    )
                else:
                    error_data = response.json() if response.text else {}
                    return TokenStatus(
                        service_name=service_name,
                        is_valid=False,
                        is_required=True,
                        error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                        error_message=f"Anthropic API returned status {response.status_code}: {error_data.get('error', {}).get('message', 'Unknown error')}",
                        last_checked=last_checked,
                    )

        except httpx.ConnectError as e:
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=True,
                error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                error_message="Cannot connect to Anthropic API. Check network.",
                last_checked=last_checked,
                details={"error": str(e)},
            )
        except httpx.TimeoutException:
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=True,
                error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                error_message=f"Anthropic API connection timed out after {self.timeout}s.",
                last_checked=last_checked,
            )
        except Exception as e:
            logger.error(f"Unexpected error validating Anthropic token: {e}")
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=True,
                error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                error_message=f"Unexpected error: {str(e)}",
                last_checked=last_checked,
            )

    async def validate_figma_token(self) -> TokenStatus:
        """
        Validate Figma personal access token.

        Returns:
            TokenStatus with validation result
        """
        service_name = "Figma"
        last_checked = datetime.now()

        if not settings.figma_token:
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=False,  # Optional service
                error_type=TokenErrorType.MISSING,
                error_message="Figma token not configured (optional). Set FIGMA_TOKEN for design context.",
                help_url="https://help.figma.com/hc/en-us/articles/8085703771159-Manage-personal-access-tokens",
                last_checked=last_checked,
            )

        # Probe the endpoint the app actually calls. /v1/me answers a
        # different question — it needs current_user:read, which this bot
        # never asks for — so it keeps reporting a healthy token while real
        # file reads are dead. On 2026-09-16 /v1/me gave its usual 403-scope
        # "token is fine" while every /v1/files call had been 429 for days and
        # design context was missing from every plan carrying a Figma link.
        probe_key = settings.figma_healthcheck_file_key
        checks_quota = bool(probe_key)
        url = f"https://api.figma.com/v1/files/{probe_key or _FIGMA_PROBE_MISS_KEY}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    url, headers={"X-FIGMA-TOKEN": settings.figma_token},
                )

                if response.status_code == 200:
                    try:
                        file_name = (response.json() or {}).get("name")
                    except ValueError:
                        file_name = None
                    return TokenStatus(
                        service_name=service_name,
                        is_valid=True,
                        is_required=False,
                        error_type=TokenErrorType.VALID,
                        last_checked=last_checked,
                        details={"file_key": probe_key, "file_name": file_name},
                    )
                elif response.status_code == 404:
                    if checks_quota:
                        # The configured file is the one thing this check is
                        # allowed to assume exists. A miss means it was deleted
                        # or this token lost access to it, and the check is now
                        # blind to the quota it was configured to watch.
                        return TokenStatus(
                            service_name=service_name,
                            is_valid=False,
                            is_required=False,
                            error_type=TokenErrorType.INSUFFICIENT_PERMISSIONS,
                            error_message=(
                                "FIGMA_HEALTHCHECK_FILE_KEY names a file this token "
                                "cannot read. Point it at a file the token can open, "
                                "or design-context health is no longer being checked."
                            ),
                            last_checked=last_checked,
                        )
                    return TokenStatus(
                        service_name=service_name,
                        is_valid=True,
                        is_required=False,
                        error_type=TokenErrorType.VALID,
                        error_message=(
                            "Token authenticates and is scoped for file reads. Quota "
                            "was NOT checked: set FIGMA_HEALTHCHECK_FILE_KEY to a real "
                            "file key to catch a spent file-read budget, which this "
                            "check cannot see without one."
                        ),
                        last_checked=last_checked,
                        details={"quota_checked": False},
                    )
                elif response.status_code == 429:
                    wait = _retry_after_seconds(response)
                    # A burst throttle clears itself in seconds and is nobody's
                    # emergency. A spent quota is: Figma's file budget recovers
                    # over days, and every plan generated meanwhile ships
                    # without design context. Only Retry-After tells them apart,
                    # so a missing header is treated as the serious case.
                    brief = wait is not None and wait <= _FIGMA_THROTTLE_TOLERANCE_S
                    return TokenStatus(
                        service_name=service_name,
                        is_valid=brief,
                        is_required=False,
                        error_type=TokenErrorType.RATE_LIMITED,
                        error_message=(
                            "Figma file reads are rate limited. The token itself is "
                            "fine, but design context is missing from every plan with "
                            "a Figma link until the limit clears — expected in "
                            f"{_humanize_seconds(wait)}."
                        ),
                        last_checked=last_checked,
                        details={"retry_after_seconds": wait},
                    )
                elif response.status_code == 401:
                    return TokenStatus(
                        service_name=service_name,
                        is_valid=False,
                        is_required=False,
                        error_type=TokenErrorType.INVALID,
                        error_message="Figma token is invalid. Please generate a new token.",
                        help_url="https://help.figma.com/hc/en-us/articles/8085703771159-Manage-personal-access-tokens",
                        last_checked=last_checked,
                    )
                elif response.status_code == 403:
                    error_text = response.text.lower()
                    if "scope" in error_text:
                        # Kept from when this probed /v1/me: a file-scoped token
                        # fails there and works everywhere the app looks. It
                        # should not reach here now, and if it does, a scope
                        # complaint still proves the token is live.
                        return TokenStatus(
                            service_name=service_name,
                            is_valid=True,
                            is_required=False,
                            error_type=TokenErrorType.VALID,
                            error_message=(
                                "Token is live and scoped for file access. Figma "
                                "rejected a scope this app does not use — design "
                                "context is unaffected."
                            ),
                            last_checked=last_checked,
                        )
                    if "rate limit" in error_text:
                        return TokenStatus(
                            service_name=service_name,
                            is_valid=True,
                            is_required=False,
                            error_type=TokenErrorType.RATE_LIMITED,
                            error_message="Figma API rate limit exceeded. Wait and try again.",
                            last_checked=last_checked,
                        )
                    if "invalid token" in error_text or "expired" in error_text:
                        return TokenStatus(
                            service_name=service_name,
                            is_valid=False,
                            is_required=False,
                            error_type=TokenErrorType.EXPIRED,
                            error_message=(
                                "Figma rejected the token outright. Generate a new "
                                "personal access token with file read scope."
                            ),
                            help_url="https://help.figma.com/hc/en-us/articles/8085703771159-Manage-personal-access-tokens",
                            last_checked=last_checked,
                        )
                    return TokenStatus(
                        service_name=service_name,
                        is_valid=False,
                        is_required=False,
                        error_type=TokenErrorType.INSUFFICIENT_PERMISSIONS,
                        error_message="Figma token lacks required permissions.",
                        help_url="https://help.figma.com/hc/en-us/articles/8085703771159-Manage-personal-access-tokens",
                        last_checked=last_checked,
                    )
                else:
                    return TokenStatus(
                        service_name=service_name,
                        is_valid=False,
                        is_required=False,
                        error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                        error_message=f"Figma API returned unexpected status {response.status_code}.",
                        last_checked=last_checked,
                    )

        except httpx.ConnectError as e:
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=False,
                error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                error_message="Cannot connect to Figma API. Check network.",
                last_checked=last_checked,
                details={"error": str(e)},
            )
        except httpx.TimeoutException:
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=False,
                error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                error_message=f"Figma API connection timed out after {self.timeout}s.",
                last_checked=last_checked,
            )
        except Exception as e:
            logger.error(f"Unexpected error validating Figma token: {e}")
            return TokenStatus(
                service_name=service_name,
                is_valid=False,
                is_required=False,
                error_type=TokenErrorType.SERVICE_UNAVAILABLE,
                error_message=f"Unexpected error: {str(e)}",
                last_checked=last_checked,
            )

    async def validate_all_tokens(self) -> list[TokenStatus]:
        """
        Validate all configured API tokens.

        Returns:
            List of TokenStatus for all services
        """
        # Validate all services concurrently
        import asyncio

        results = await asyncio.gather(
            self.validate_jira_token(),
            self.validate_github_token(),
            self.validate_anthropic_token(),
            self.validate_figma_token(),
            return_exceptions=True,
        )

        # Filter out any exceptions and return valid results
        token_statuses = []
        for result in results:
            if isinstance(result, TokenStatus):
                token_statuses.append(result)
            else:
                logger.error(f"Error during token validation: {result}")

        return token_statuses


# Singleton instance
token_health_service = TokenHealthService()
