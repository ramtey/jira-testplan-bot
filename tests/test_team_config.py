"""Tests for the team-specific values that moved from code into config.

These used to be literals in `jira_client` and `bug_lens_routes`, so a clone of
this repo inherited one team's coworkers and repos. What matters now is that
each one reads from settings, and that an unconfigured install degrades in a
way you can see rather than silently mis-assigning tickets.
"""

import logging

import pytest

from src.app import bug_lens_routes, jira_client
from src.app.config import settings


@pytest.fixture
def no_team_config(monkeypatch):
    """An install that has configured none of it — a fresh clone."""
    monkeypatch.setattr(settings, "team_github_login_to_jira", {}, raising=False)
    monkeypatch.setattr(settings, "bot_display_names", [], raising=False)
    monkeypatch.setattr(settings, "bug_lens_repo_hints", {}, raising=False)
    monkeypatch.setattr(jira_client, "_warned_empty_team_map", False, raising=False)


# ---- Fail-back assignee mapping ------------------------------------------

def test_configured_login_resolves_to_a_jira_user(monkeypatch):
    monkeypatch.setattr(
        settings,
        "team_github_login_to_jira",
        {"octocat": ("557058:abc", "Octo Cat")},
        raising=False,
    )
    assert jira_client.team_jira_user_for_github_login("octocat") == (
        "557058:abc",
        "Octo Cat",
    )


def test_an_unknown_login_is_a_miss_not_a_crash(monkeypatch):
    monkeypatch.setattr(
        settings,
        "team_github_login_to_jira",
        {"octocat": ("557058:abc", "Octo Cat")},
        raising=False,
    )
    assert jira_client.team_jira_user_for_github_login("someone-else") is None


def test_an_empty_map_says_so_once(no_team_config, caplog):
    """The miss is indistinguishable from "not on the team", so it must be logged.

    Without this, an install that never set TEAM_GITHUB_LOGIN_TO_JIRA looks
    identical to one where the PR author simply isn't mapped — and the
    fail-back quietly assigns the wrong person or nobody.
    """
    with caplog.at_level(logging.WARNING, logger=jira_client.__name__):
        assert jira_client.team_jira_user_for_github_login("octocat") is None
        assert jira_client.team_jira_user_for_github_login("another") is None

    warnings = [r for r in caplog.records if "TEAM_GITHUB_LOGIN_TO_JIRA" in r.message]
    assert len(warnings) == 1, "should warn once per process, not once per lookup"


# ---- Bot accounts --------------------------------------------------------

def test_configured_bot_name_is_blocked_case_insensitively(monkeypatch):
    monkeypatch.setattr(settings, "bot_display_names", ["Testing Acme"], raising=False)
    assert jira_client.is_blocked_bot_display_name("testing acme") is True
    assert jira_client.is_blocked_bot_display_name("  TESTING ACME  ") is True
    assert jira_client.is_blocked_bot_display_name("Octo Cat") is False


def test_no_configured_bots_blocks_nobody(no_team_config):
    assert jira_client.is_blocked_bot_display_name("Testing Acme") is False
    assert jira_client.is_blocked_bot_display_name(None) is False


# ---- Bug Lens repo hints -------------------------------------------------

def test_repo_hints_come_from_config(monkeypatch):
    monkeypatch.setattr(
        settings,
        "bug_lens_repo_hints",
        {"agent.?cal(culator)?": ["acme/agent-calculator"]},
        raising=False,
    )
    assert bug_lens_routes._infer_repos_from_text("Agent Calculator shows wrong tax") == [
        "acme/agent-calculator"
    ]


def test_no_hints_means_no_repos_are_guessed(no_team_config):
    """There are no built-in hints any more — one team's repo names were wrong
    for every other install, and searching a repo you don't own just burns
    GitHub's code-search budget."""
    assert bug_lens_routes._infer_repos_from_text("Agent Calculator shows wrong tax") == []


def test_an_invalid_hint_regex_is_survivable(monkeypatch, caplog):
    monkeypatch.setattr(
        settings,
        "bug_lens_repo_hints",
        {"agent(": ["acme/agent-calculator"], "coach": ["acme/agent-coach"]},
        raising=False,
    )
    with caplog.at_level(logging.WARNING, logger=bug_lens_routes.__name__):
        # The good pattern still contributes; the broken one is reported.
        assert bug_lens_routes._infer_repos_from_text("coach page broken") == [
            "acme/agent-coach"
        ]
    assert any("Invalid regex" in r.message for r in caplog.records)
