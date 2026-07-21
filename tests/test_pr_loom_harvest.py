"""
Tests for the opt-in `_harvest_loom_urls_from_merged_prs` helper that
lets Pass-to-UAT fold Loom URLs from a ticket's merged PR descriptions
into the hand-off comment.

Scope decisions the helper enforces (see workflow_routes for rationale):
  - Only runs when settings.github_token is configured.
  - Description only — never PR review comments.
  - Merged PRs only — draft/closed PRs are ignored.
  - Silent on failure — never blocks the Pass-to-UAT transition.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app import workflow_routes


def _pr_details(description: str | None, merged: bool = True):
    """Minimal PRDetails stand-in — the harvester only reads .description and .merged."""
    stub = MagicMock()
    stub.description = description
    stub.merged = merged
    return stub


def _jira_client(*, issue_id: str | None = "10001", pr_urls: list[str] | None = None):
    """Build a JiraClient stub with the two private methods the harvester calls."""
    if pr_urls is None:
        pr_urls = []
    jira = MagicMock()
    jira._get_issue_internal_id = AsyncMock(return_value=issue_id)
    jira._list_dev_status_pr_urls = AsyncMock(return_value=pr_urls)
    return jira


# ---------- Regex behavior ----------


def test_loom_regex_matches_share_urls():
    assert workflow_routes._LOOM_URL_RE.findall(
        "Demo: https://www.loom.com/share/abc123 and https://loom.com/share/DEF-456_x"
    ) == [
        "https://www.loom.com/share/abc123",
        "https://loom.com/share/DEF-456_x",
    ]


def test_loom_regex_is_case_insensitive():
    assert workflow_routes._LOOM_URL_RE.findall(
        "See HTTPS://LOOM.COM/share/abc"
    ) == ["HTTPS://LOOM.COM/share/abc"]


def test_loom_regex_ignores_non_share_paths():
    # Only /share/<slug> — /looms/, /embed/, /profile/ etc. don't count.
    assert workflow_routes._LOOM_URL_RE.findall(
        "Not a Loom: https://loom.com/looms/abc or https://loom.com/embed/abc"
    ) == []


# ---------- Guards ----------


@pytest.mark.asyncio
async def test_returns_empty_when_github_token_missing():
    """No token → skip the whole thing; Jira is never even queried."""
    jira = _jira_client()
    with patch.object(workflow_routes.settings, "github_token", None):
        assert await workflow_routes._harvest_loom_urls_from_merged_prs(jira, "SK-1") == []
    jira._get_issue_internal_id.assert_not_called()


@pytest.mark.asyncio
async def test_returns_empty_when_issue_id_not_resolved():
    jira = _jira_client(issue_id=None)
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"):
        assert await workflow_routes._harvest_loom_urls_from_merged_prs(jira, "SK-1") == []
    jira._list_dev_status_pr_urls.assert_not_called()


@pytest.mark.asyncio
async def test_returns_empty_when_no_prs_linked():
    jira = _jira_client(pr_urls=[])
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"):
        assert await workflow_routes._harvest_loom_urls_from_merged_prs(jira, "SK-1") == []


@pytest.mark.asyncio
async def test_ignores_non_github_pr_urls():
    """Bitbucket/GitLab PRs never reach GitHubClient."""
    jira = _jira_client(pr_urls=["https://bitbucket.org/foo/bar/pull-requests/1"])
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock()
        assert await workflow_routes._harvest_loom_urls_from_merged_prs(jira, "SK-1") == []
        gh_cls.return_value.fetch_pr_details.assert_not_called()


# ---------- Merge filter ----------


@pytest.mark.asyncio
async def test_skips_unmerged_prs():
    """Only merged=True PRs contribute Loom URLs."""
    jira = _jira_client(pr_urls=["https://github.com/o/r/pull/1"])
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock(
            return_value=_pr_details("https://loom.com/share/x", merged=False)
        )
        assert await workflow_routes._harvest_loom_urls_from_merged_prs(jira, "SK-1") == []


# ---------- Happy path ----------


@pytest.mark.asyncio
async def test_extracts_loom_url_from_merged_pr_body():
    jira = _jira_client(pr_urls=["https://github.com/o/r/pull/1"])
    body = "See demo: https://www.loom.com/share/abc123 (30s walkthrough)."
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock(return_value=_pr_details(body))
        assert await workflow_routes._harvest_loom_urls_from_merged_prs(jira, "SK-1") == [
            "https://www.loom.com/share/abc123"
        ]


@pytest.mark.asyncio
async def test_strips_trailing_punctuation_from_matches():
    """Regex is greedy on word chars — trailing period/paren/etc. get stripped
    so link text stays clickable in the Jira comment."""
    jira = _jira_client(pr_urls=["https://github.com/o/r/pull/1"])
    body = "video: https://loom.com/share/abc123. also (https://loom.com/share/def456);"
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock(return_value=_pr_details(body))
        urls = await workflow_routes._harvest_loom_urls_from_merged_prs(jira, "SK-1")
        assert urls == [
            "https://loom.com/share/abc123",
            "https://loom.com/share/def456",
        ]


@pytest.mark.asyncio
async def test_dedups_same_loom_across_multiple_prs():
    """Two merged PRs quoting the same Loom → one entry, first-seen wins."""
    jira = _jira_client(
        pr_urls=[
            "https://github.com/o/r/pull/1",
            "https://github.com/o/r/pull/2",
        ]
    )
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock(
            side_effect=[
                _pr_details("https://loom.com/share/dup"),
                _pr_details("https://loom.com/share/dup and https://loom.com/share/new"),
            ]
        )
        urls = await workflow_routes._harvest_loom_urls_from_merged_prs(jira, "SK-1")
        assert urls == [
            "https://loom.com/share/dup",
            "https://loom.com/share/new",
        ]


@pytest.mark.asyncio
async def test_returns_empty_when_merged_pr_body_has_no_loom():
    jira = _jira_client(pr_urls=["https://github.com/o/r/pull/1"])
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock(
            return_value=_pr_details("Fixed a null-pointer in the auth layer.")
        )
        assert await workflow_routes._harvest_loom_urls_from_merged_prs(jira, "SK-1") == []


@pytest.mark.asyncio
async def test_handles_pr_with_null_description():
    """GitHub returns body=None for PRs opened without a description."""
    jira = _jira_client(pr_urls=["https://github.com/o/r/pull/1"])
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock(
            return_value=_pr_details(None)
        )
        assert await workflow_routes._harvest_loom_urls_from_merged_prs(jira, "SK-1") == []


# ---------- Failure isolation ----------


@pytest.mark.asyncio
async def test_swallows_exception_from_issue_id_lookup():
    """A Jira lookup blowing up must not surface — the caller keeps the transition."""
    jira = MagicMock()
    jira._get_issue_internal_id = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"):
        assert await workflow_routes._harvest_loom_urls_from_merged_prs(jira, "SK-1") == []


@pytest.mark.asyncio
async def test_swallows_exception_from_pr_url_lookup():
    jira = _jira_client()
    jira._list_dev_status_pr_urls = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"):
        assert await workflow_routes._harvest_loom_urls_from_merged_prs(jira, "SK-1") == []


@pytest.mark.asyncio
async def test_ignores_prs_where_github_fetch_fails():
    """One bad PR fetch (exception or None) doesn't taint the others."""
    jira = _jira_client(
        pr_urls=[
            "https://github.com/o/r/pull/1",
            "https://github.com/o/r/pull/2",
            "https://github.com/o/r/pull/3",
        ]
    )
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock(
            side_effect=[
                RuntimeError("rate limited"),
                None,
                _pr_details("Demo: https://loom.com/share/ok"),
            ]
        )
        assert await workflow_routes._harvest_loom_urls_from_merged_prs(jira, "SK-1") == [
            "https://loom.com/share/ok"
        ]
