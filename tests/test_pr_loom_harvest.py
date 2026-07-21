"""
Tests for the PR-Loom discovery path used by Pass-to-UAT:

  - `_harvest_loom_urls_from_merged_prs`: the underlying helper. Scans
    the description of each merged PR linked to the ticket and pulls
    out loom.com share URLs, returning (urls, status) so callers can
    show the tester *why* the list is empty when it is.
  - `GET /issue/{key}/pr-looms`: the preview endpoint the frontend calls
    when the Pass-to-UAT modal opens.

Scope decisions the helper enforces (see workflow_routes for rationale):
  - Only runs when settings.github_token is configured.
  - Description only — never PR review comments.
  - Merged PRs only — draft/closed PRs are ignored.
  - Silent on failure — status='error' signals the caller, never raises.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.app import workflow_routes
from src.app.main import app


def _pr_details(description: str | None, merged: bool = True):
    """Minimal PRDetails stand-in — the harvester only reads .description and .merged."""
    stub = MagicMock()
    stub.description = description
    stub.merged = merged
    return stub


def _jira_client(
    *,
    issue_id: str | None = "10001",
    pr_urls: list[str] | None = None,
    pr_rows: list[dict] | None = None,
):
    """Build a JiraClient stub with the private methods the harvester calls.

    `pr_urls` is a convenience for the common case where every PR is MERGED.
    `pr_rows` lets a test hand-craft the (url, status) list for cases where
    merge state matters (e.g., mixed MERGED / DECLINED / OPEN).
    """
    if pr_rows is None:
        pr_rows = [
            {"url": url, "status": "MERGED"} for url in (pr_urls or [])
        ]
    jira = MagicMock()
    jira._get_issue_internal_id = AsyncMock(return_value=issue_id)
    jira._list_dev_status_pr_summaries = AsyncMock(return_value=pr_rows)
    return jira


async def _harvest(jira, key="SK-1"):
    """Shorthand — every test call the same way."""
    return await workflow_routes._harvest_loom_urls_from_merged_prs(jira, key)


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
async def test_status_no_token_when_github_token_missing():
    """No token → skip the whole thing; Jira is never even queried."""
    jira = _jira_client()
    with patch.object(workflow_routes.settings, "github_token", None):
        assert await _harvest(jira) == ([], "no_token")
    jira._get_issue_internal_id.assert_not_called()


@pytest.mark.asyncio
async def test_status_no_prs_when_issue_id_not_resolved():
    """Issue key that doesn't map to an internal id → treat as no PRs
    (indistinguishable from a real 'no PRs linked' state at this layer)."""
    jira = _jira_client(issue_id=None)
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"):
        assert await _harvest(jira) == ([], "no_prs")
    jira._list_dev_status_pr_summaries.assert_not_called()


@pytest.mark.asyncio
async def test_status_no_prs_when_dev_status_returns_none():
    jira = _jira_client(pr_urls=[])
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"):
        assert await _harvest(jira) == ([], "no_prs")


@pytest.mark.asyncio
async def test_status_no_prs_when_only_non_github_pr_urls():
    """Bitbucket/GitLab PRs never reach GitHubClient and count as 'no PRs'
    from the harvester's POV — we can't inspect their body."""
    jira = _jira_client(pr_urls=["https://bitbucket.org/foo/bar/pull-requests/1"])
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock()
        assert await _harvest(jira) == ([], "no_prs")
        gh_cls.return_value.fetch_pr_details.assert_not_called()


# ---------- Merge filter ----------


@pytest.mark.asyncio
async def test_status_no_merged_prs_when_jira_says_none_merged():
    """PRs exist but Jira's dev-status marks them all OPEN/DECLINED — the
    merge check happens *before* the GitHub call, so no fetch is issued."""
    jira = _jira_client(pr_rows=[
        {"url": "https://github.com/o/r/pull/1", "status": "OPEN"},
        {"url": "https://github.com/o/r/pull/2", "status": "DECLINED"},
    ])
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock()
        assert await _harvest(jira) == ([], "no_merged_prs")
        gh_cls.return_value.fetch_pr_details.assert_not_called()


@pytest.mark.asyncio
async def test_declined_pr_is_ignored_but_merged_sibling_yields_looms():
    """The real motivating case: a ticket with several merged PRs and one
    declined one. The declined PR is filtered out server-side; the merged
    PRs' Loom URLs come through."""
    jira = _jira_client(pr_rows=[
        {"url": "https://github.com/o/r/pull/1", "status": "MERGED"},
        {"url": "https://github.com/o/r/pull/2", "status": "DECLINED"},
        {"url": "https://github.com/o/r/pull/3", "status": "MERGED"},
    ])
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock(
            side_effect=[
                _pr_details("Demo: https://loom.com/share/one"),
                _pr_details("Follow-up: https://loom.com/share/two"),
            ]
        )
        urls, status = await _harvest(jira)
        assert status == "found"
        assert urls == [
            "https://loom.com/share/one",
            "https://loom.com/share/two",
        ]
        # Only the two MERGED PRs get a GitHub fetch — the DECLINED one skipped early.
        assert gh_cls.return_value.fetch_pr_details.await_count == 2


@pytest.mark.asyncio
async def test_status_github_unreachable_when_every_merged_pr_fetch_fails():
    """Merged PRs exist per Jira, but GitHub can't be reached for any of them
    (rate limit, permissions, transient 5xx). Distinct from 'no merged PRs'
    so the tester knows to retry / check credentials, not chase the dev team."""
    jira = _jira_client(pr_rows=[
        {"url": "https://github.com/o/r/pull/1", "status": "MERGED"},
        {"url": "https://github.com/o/r/pull/2", "status": "MERGED"},
    ])
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock(
            side_effect=[None, RuntimeError("rate limited")]
        )
        assert await _harvest(jira) == ([], "github_unreachable")


# ---------- Happy path ----------


@pytest.mark.asyncio
async def test_status_found_with_loom_in_merged_pr_body():
    jira = _jira_client(pr_urls=["https://github.com/o/r/pull/1"])
    body = "See demo: https://www.loom.com/share/abc123 (30s walkthrough)."
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock(return_value=_pr_details(body))
        assert await _harvest(jira) == (
            ["https://www.loom.com/share/abc123"],
            "found",
        )


@pytest.mark.asyncio
async def test_strips_trailing_punctuation_from_matches():
    """Regex is greedy on word chars — trailing period/paren/etc. get stripped
    so link text stays clickable in the Jira comment."""
    jira = _jira_client(pr_urls=["https://github.com/o/r/pull/1"])
    body = "video: https://loom.com/share/abc123. also (https://loom.com/share/def456);"
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock(return_value=_pr_details(body))
        urls, status = await _harvest(jira)
        assert status == "found"
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
        urls, status = await _harvest(jira)
        assert status == "found"
        assert urls == [
            "https://loom.com/share/dup",
            "https://loom.com/share/new",
        ]


@pytest.mark.asyncio
async def test_status_no_looms_when_merged_pr_body_has_none():
    jira = _jira_client(pr_urls=["https://github.com/o/r/pull/1"])
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock(
            return_value=_pr_details("Fixed a null-pointer in the auth layer.")
        )
        assert await _harvest(jira) == ([], "no_looms")


@pytest.mark.asyncio
async def test_status_no_looms_when_merged_pr_has_null_description():
    """GitHub returns body=None for PRs opened without a description."""
    jira = _jira_client(pr_urls=["https://github.com/o/r/pull/1"])
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"), \
            patch.object(workflow_routes, "GitHubClient") as gh_cls:
        gh_cls.return_value.fetch_pr_details = AsyncMock(
            return_value=_pr_details(None)
        )
        assert await _harvest(jira) == ([], "no_looms")


# ---------- Failure isolation ----------


@pytest.mark.asyncio
async def test_status_error_when_issue_id_lookup_raises():
    """A Jira lookup blowing up surfaces as 'error' — never as an exception."""
    jira = MagicMock()
    jira._get_issue_internal_id = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"):
        assert await _harvest(jira) == ([], "error")


@pytest.mark.asyncio
async def test_status_error_when_pr_url_lookup_raises():
    jira = _jira_client()
    jira._list_dev_status_pr_summaries = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(workflow_routes.settings, "github_token", "gh-fake"):
        assert await _harvest(jira) == ([], "error")


@pytest.mark.asyncio
async def test_per_pr_failures_dont_taint_batch():
    """One bad PR fetch (exception or None) doesn't taint the others.
    The batch survives with 'found' as long as at least one succeeds."""
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
        assert await _harvest(jira) == (
            ["https://loom.com/share/ok"],
            "found",
        )


# ---------- GET /issue/{key}/pr-looms endpoint ----------

_endpoint_client = TestClient(app)


def test_pr_looms_endpoint_returns_urls_and_status():
    """The endpoint is a thin wrapper — mock the harvester and verify shape."""
    with patch.object(
        workflow_routes,
        "_harvest_loom_urls_from_merged_prs",
        new=AsyncMock(return_value=(
            ["https://loom.com/share/one", "https://loom.com/share/two"],
            "found",
        )),
    ):
        response = _endpoint_client.get("/issue/SK-123/pr-looms")
    assert response.status_code == 200
    assert response.json() == {
        "loom_urls": [
            "https://loom.com/share/one",
            "https://loom.com/share/two",
        ],
        "status": "found",
    }


def test_pr_looms_endpoint_passes_status_through_when_empty():
    """no_looms / no_prs / no_token all come back as 200 with their status —
    the frontend uses them to explain *why* nothing was found."""
    with patch.object(
        workflow_routes,
        "_harvest_loom_urls_from_merged_prs",
        new=AsyncMock(return_value=([], "no_looms")),
    ):
        response = _endpoint_client.get("/issue/SK-999/pr-looms")
    assert response.status_code == 200
    assert response.json() == {"loom_urls": [], "status": "no_looms"}


def test_pr_looms_endpoint_skips_non_sk_projects_with_skipped_status():
    """SK-project gate matches the workflow POST. Non-SK returns status='skipped'
    so the frontend can render nothing at all (silent for foreign projects)."""
    with patch.object(
        workflow_routes,
        "_harvest_loom_urls_from_merged_prs",
        new=AsyncMock(return_value=(["https://loom.com/share/should-not-appear"], "found")),
    ) as harvest:
        response = _endpoint_client.get("/issue/FOO-1/pr-looms")
    assert response.status_code == 200
    assert response.json() == {"loom_urls": [], "status": "skipped"}
    harvest.assert_not_called()


def test_pr_looms_endpoint_accepts_lowercase_project_key():
    """URLs sometimes come through lowercased (link normalization,
    copy/paste). The gate should still allow SK-anything through."""
    with patch.object(
        workflow_routes,
        "_harvest_loom_urls_from_merged_prs",
        new=AsyncMock(return_value=(["https://loom.com/share/x"], "found")),
    ):
        response = _endpoint_client.get("/issue/sk-1/pr-looms")
    assert response.status_code == 200
    assert response.json() == {
        "loom_urls": ["https://loom.com/share/x"],
        "status": "found",
    }
