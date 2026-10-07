"""Full changed files beside the diff, behind `full_file_grounding`.

Why this file exists
--------------------
A diff hunk shows what changed, not what the code now does: on SK-2687 the
handler's `403 "Only the envelope's owner…"` sat outside every hunk, so the
plan hedged each API case as "unverified". Showing the whole file at the PR's
head commit fixed that in a controlled rerun (rubric ~16 → ~22 of 32).

These tests pin the parts that can go quietly wrong: the setting really gates
the fetch (it multiplies prompt size), a file GitHub would not return stays
absent rather than becoming an empty file, the prompt carries line numbers a
case can cite, and the per-ticket budget is shared rather than per PR.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.app import jira_client
from src.app.config import settings
from src.app.diff_budget import render_full_files
from src.app.models import FileChange, PullRequest
from tests.test_bounce_in_prompts import _PromptOnlyClient

HANDLER = "Api/StartInPersonSessionHandler.cs"
HANDLER_TEXT = "\n".join(
    ["using X;"] * 73
    + ['return Error(HttpStatusCode.Forbidden, "Only the envelope\'s owner can start an in-person signing session.");']
)


def _pr(**over):
    pr = PullRequest(
        title="Owner-only in-person", status="MERGED",
        url="https://github.com/acme/api/pull/92", repository="acme/api", head_sha="6d9a355abc",
        files_changed=[
            FileChange(HANDLER, "modified", 10, 2, 12, patch="@@ -70,3 +70,4 @@\n+guard"),
            FileChange("Api/Removed.cs", "removed", 0, 5, 5, patch="@@ -1 +0,0 @@\n-gone"),
            FileChange("docs/threat-model.md", "modified", 1, 1, 2, patch=None),
        ],
    )
    for k, v in over.items():
        setattr(pr, k, v)
    return pr


# ---------------------------------------------------------------- enrichment

@pytest.mark.asyncio
async def test_off_by_default_fetches_nothing(monkeypatch):
    monkeypatch.setattr(settings, "full_file_grounding", False)
    gh = AsyncMock()
    pr = _pr()
    await jira_client._attach_full_files(gh, pr)
    gh.fetch_files_at_ref.assert_not_called()
    assert all(fc.full_content is None for fc in pr.files_changed)


@pytest.mark.asyncio
async def test_on_fetches_diffed_files_at_head_commit_only(monkeypatch):
    monkeypatch.setattr(settings, "full_file_grounding", True)
    gh = AsyncMock()
    gh.fetch_files_at_ref.return_value = {HANDLER: HANDLER_TEXT}
    pr = _pr()
    await jira_client._attach_full_files(gh, pr)
    # Removed files and files with no diff (docs, config) are not fetched.
    gh.fetch_files_at_ref.assert_awaited_once_with("acme/api", "6d9a355abc", [HANDLER])
    assert pr.files_changed[0].full_content == HANDLER_TEXT


@pytest.mark.asyncio
async def test_a_file_github_would_not_return_stays_absent(monkeypatch):
    monkeypatch.setattr(settings, "full_file_grounding", True)
    gh = AsyncMock()
    gh.fetch_files_at_ref.return_value = {}
    pr = _pr()
    await jira_client._attach_full_files(gh, pr)
    # None, never "": an empty file would read as "the handler has no guard".
    assert pr.files_changed[0].full_content is None


@pytest.mark.asyncio
async def test_no_head_sha_means_no_fetch(monkeypatch):
    monkeypatch.setattr(settings, "full_file_grounding", True)
    gh = AsyncMock()
    await jira_client._attach_full_files(gh, _pr(head_sha=None))
    gh.fetch_files_at_ref.assert_not_called()


# ---------------------------------------------------------------- rendering

def test_render_numbers_lines_so_cases_can_cite_them():
    text = render_full_files([{"filename": HANDLER, "full_content": HANDLER_TEXT}], total_budget=100_000)
    assert "   74| return Error(HttpStatusCode.Forbidden" in text
    assert f"--- {HANDLER} (whole file after this PR) ---" in text


def test_render_says_when_a_file_was_cut():
    text = render_full_files([{"filename": HANDLER, "full_content": HANDLER_TEXT}], total_budget=700)
    assert "file continues" in text
    assert "shown only in part" in text


def test_render_without_full_content_is_empty():
    assert render_full_files([{"filename": HANDLER, "patch": "x"}], total_budget=100_000) == ""


def _dev_info(*prs):
    from dataclasses import asdict
    return {"pull_requests": [asdict(p) for p in prs], "commits": [], "branches": []}


def test_prompt_carries_full_files_and_the_cite_instruction(monkeypatch):
    monkeypatch.setattr(settings, "full_file_budget_chars", 200_000)
    pr = _pr()
    pr.files_changed[0].full_content = HANDLER_TEXT
    prompt = _PromptOnlyClient()._build_prompt("SK-1", "Owner only", "body", {}, _dev_info(pr))
    assert "Changed files in full" in prompt
    assert "Only the envelope's owner can start" in prompt
    assert "cite the exact line in `expected_source`" in prompt


def test_prompt_without_full_content_is_unchanged():
    prompt = _PromptOnlyClient()._build_prompt("SK-1", "Owner only", "body", {}, _dev_info(_pr()))
    assert "Changed files in full" not in prompt


def test_budget_is_per_ticket_not_per_pr(monkeypatch):
    monkeypatch.setattr(settings, "full_file_budget_chars", 4_000)
    big = "\n".join(f"line {i} " + "x" * 40 for i in range(2_000))
    prs = []
    for n in range(4):
        pr = _pr(url=f"https://github.com/acme/api/pull/{n}")
        pr.files_changed[0].full_content = big
        prs.append(pr)
    prompt = _PromptOnlyClient()._build_prompt("SK-1", "Owner only", "body", {}, _dev_info(*prs))
    shown = sum(len(l) for l in prompt.splitlines() if "| line " in l)
    assert shown <= 4_000 + 4 * 200  # one ticket's budget, plus line-prefix slack
