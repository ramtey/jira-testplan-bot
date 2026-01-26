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
class FileChange:
    """Represents a file change in a pull request."""

    filename: str
    status: str  # "added", "modified", "removed", "renamed"
    additions: int
    deletions: int
    changes: int


@dataclass
class PRComment:
    """Represents a comment on a pull request."""

    author: str
    body: str
    created_at: str
    comment_type: str  # "conversation" or "review_comment"


@dataclass
class PullRequest:
    """Represents a pull request linked to a Jira issue."""

    title: str
    status: str
    url: str | None = None
    source_branch: str | None = None
    destination_branch: str | None = None
    # GitHub enrichment (Phase 3a)
    github_description: str | None = None
    files_changed: list[FileChange] | None = None
    total_additions: int | None = None
    total_deletions: int | None = None
    comments: list[PRComment] | None = None


@dataclass
class RepositoryContext:
    """Repository documentation and context for test plan generation."""

    readme_content: str | None = None
    test_examples: list[str] | None = None  # Paths to example test files


@dataclass
class DevelopmentInfo:
    """Development information (commits, PRs, branches) for a Jira issue."""

    commits: list[Commit]
    pull_requests: list[PullRequest]
    branches: list[str]
    repository_context: RepositoryContext | None = None  # Repository documentation


@dataclass
class Attachment:
    """Represents an attachment on a Jira issue."""

    filename: str
    mime_type: str
    size: int  # bytes
    url: str
    thumbnail_url: str | None = None


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
    attachments: list[Attachment] | None = None


# ============================================================================
# LLM-related models
# ============================================================================


@dataclass
class TestPlan:
    """Structured test plan output from LLM."""

    happy_path: list[dict]
    edge_cases: list[dict]
    regression_checklist: list[str]
    integration_tests: list[dict] | None = None  # New: Optional integration tests


# ============================================================================
# API request/response models
# ============================================================================


class GenerateTestPlanRequest(BaseModel):
    """Request body for generating test plans."""

    ticket_key: str
    summary: str
    description: str | None = None
    issue_type: str
    testing_context: dict = {}
    development_info: dict | None = None
    image_urls: list[str] | None = None  # URLs of images to download and analyze


class PostCommentRequest(BaseModel):
    """Request body for posting a comment to Jira."""

    issue_key: str
    comment_text: str
