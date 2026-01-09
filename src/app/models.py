"""
Data models for the Jira Test Plan Bot.

This module contains all Pydantic and dataclass models used throughout the application.
"""

from dataclasses import dataclass

from pydantic import BaseModel


# ============================================================================
# Jira-related models
# ============================================================================


@dataclass
class DescriptionAnalysis:
    """Analysis results for a Jira issue description."""

    has_description: bool
    is_weak: bool
    warnings: list[str]
    char_count: int
    word_count: int


@dataclass
class Commit:
    """Represents a commit linked to a Jira issue."""

    message: str
    author: str | None = None
    date: str | None = None
    url: str | None = None


@dataclass
class PullRequest:
    """Represents a pull request linked to a Jira issue."""

    title: str
    status: str
    url: str | None = None
    source_branch: str | None = None
    destination_branch: str | None = None


@dataclass
class DevelopmentInfo:
    """Development information (commits, PRs, branches) for a Jira issue."""

    commits: list[Commit]
    pull_requests: list[PullRequest]
    branches: list[str]


@dataclass
class JiraIssue:
    """Represents a Jira issue with extracted data."""

    key: str
    summary: str
    description: str | None
    description_analysis: DescriptionAnalysis
    labels: list[str]
    issue_type: str
    development_info: DevelopmentInfo | None = None


# ============================================================================
# LLM-related models
# ============================================================================


@dataclass
class TestPlan:
    """Structured test plan output from LLM."""

    happy_path: list[dict]
    edge_cases: list[dict]
    regression_checklist: list[str]
    non_functional: list[str]
    assumptions: list[str]
    questions: list[str]


# ============================================================================
# API request/response models
# ============================================================================


class GenerateTestPlanRequest(BaseModel):
    """Request body for generating test plans."""

    ticket_key: str
    summary: str
    description: str | None = None
    testing_context: dict = {}
    development_info: dict | None = None
