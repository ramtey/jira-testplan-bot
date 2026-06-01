"""
Tests for the Jira browser methods on JiraClient (list_projects, list_project_statuses,
search_project_issues). Focused on validation and JQL escaping — the only logic in
these wrappers that could regress silently and has security implications.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.jira_client import JiraClient


@pytest.mark.asyncio
async def test_list_project_statuses_rejects_malformed_project_key():
    """Project keys must match ^[A-Z][A-Z0-9_]*$ — anything else is rejected before
    we hit Jira, so a hostile path segment can't be smuggled into the URL."""
    client = JiraClient()
    for bad_key in ["proj", "PROJ-1", "PROJ;DROP", "../etc", "PR OJ", ""]:
        with pytest.raises(ValueError):
            await client.list_project_statuses(bad_key)


@pytest.mark.asyncio
async def test_search_project_issues_rejects_malformed_project_key():
    """Same project-key guard on the search method — important because the key is
    interpolated directly into the JQL string."""
    client = JiraClient()
    for bad_key in ["proj", "PROJ-1", 'PROJ" OR ""="', "../etc", ""]:
        with pytest.raises(ValueError):
            await client.search_project_issues(bad_key, "In Progress")


@pytest.mark.asyncio
async def test_search_project_issues_escapes_quotes_and_backslashes_in_status():
    """Status names are user-facing strings (e.g. 'In Progress') that get interpolated
    into JQL. A status containing `"` or `\\` must be escaped, otherwise a hostile name
    could break out of the quoted literal and inject JQL."""
    client = JiraClient()

    with patch("httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"issues": [], "nextPageToken": None}
        mock_post = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__.return_value.post = mock_post

        # Status name containing both a backslash and a double quote.
        await client.search_project_issues("PROJ", 'Done\\" OR status = "Open')

        # The implementation fires two POSTs in parallel: the main status search
        # and a sprint-usage probe. Pick out the main one by its JQL shape.
        main_jql = next(
            call.kwargs["json"]["jql"]
            for call in mock_post.call_args_list
            if "status = " in call.kwargs["json"]["jql"]
        )

        # The injected `"` must be escaped (`\"`), and the literal backslash doubled.
        # The whole status must remain a single quoted JQL literal — no unescaped `"`
        # should appear in the middle of the value.
        assert main_jql == (
            'project = PROJ AND status = "Done\\\\\\" OR status = \\"Open" '
            'ORDER BY Rank ASC, created ASC'
        )
