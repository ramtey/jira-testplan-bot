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
    patch: str | None = None  # Diff patch for runtime source files (config/tooling excluded)


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
    repository: str | None = None  # e.g. "owner/repo" extracted from URL
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
    testid_reference: str | None = None     # Auto-generated testID map (from .agents/skills/simulator-testing/references/testid-reference.md)
    screen_guide: str | None = None         # Screen navigation guide (from .agents/skills/simulator-testing/references/screen-guide.md)


@dataclass
class FigmaFrame:
    """Represents a frame or page in a Figma file."""

    name: str
    type: str  # "FRAME", "COMPONENT", "PAGE", etc.
    node_id: str | None = None
    description: str | None = None


@dataclass
class FigmaComponent:
    """Represents a component in a Figma file."""

    name: str
    description: str | None
    component_set_name: str | None = None  # For variants


@dataclass
class FigmaContext:
    """Figma design context for test plan generation."""

    file_name: str
    file_key: str
    last_modified: str | None = None
    frames: list[FigmaFrame] | None = None  # Top-level frames/pages
    components: list[FigmaComponent] | None = None  # Reusable components
    version: str | None = None  # File version info


@dataclass
class DevelopmentInfo:
    """Development information (commits, PRs, branches) for a Jira issue."""

    commits: list[Commit]
    pull_requests: list[PullRequest]
    branches: list[str]
    repository_context: RepositoryContext | None = None  # Repository documentation
    figma_context: FigmaContext | None = None  # Figma design context


@dataclass
class JiraComment:
    """Represents a comment on a Jira issue."""

    author: str
    body: str  # Plain text extracted from ADF
    created: str
    updated: str | None = None


@dataclass
class Attachment:
    """Represents an attachment on a Jira issue."""

    filename: str
    mime_type: str
    size: int  # bytes
    url: str
    thumbnail_url: str | None = None


@dataclass
class ParentIssue:
    """Represents the parent issue of a sub-task with design resources."""

    key: str
    summary: str
    description: str | None
    issue_type: str
    labels: list[str]
    attachments: list[Attachment] | None = None  # Images from parent ticket
    figma_context: FigmaContext | None = None    # Figma designs from parent ticket


@dataclass
class LinkedIssue:
    """Represents a linked issue (blocks, is blocked by, etc.)."""

    key: str
    summary: str
    description: str | None
    issue_type: str
    link_type: str  # "blocks", "is_blocked_by", "causes", "is_caused_by", etc.
    status: str | None = None  # Current status of the linked issue


@dataclass
class LinkedIssues:
    """Container for all linked issues, organized by link type."""

    blocks: list[LinkedIssue] | None = None  # Issues this ticket blocks
    blocked_by: list[LinkedIssue] | None = None  # Issues blocking this ticket
    causes: list[LinkedIssue] | None = None  # Issues this ticket causes
    caused_by: list[LinkedIssue] | None = None  # Issues that caused this ticket


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
    comments: list[JiraComment] | None = None  # Filtered testing-related comments
    parent: ParentIssue | None = None  # Parent ticket context with design resources
    linked_issues: LinkedIssues | None = None  # Linked tickets (blocks, blocked by, etc.)


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
    comments: list[dict] | None = None  # Filtered testing-related Jira comments
    parent_info: dict | None = None  # Parent ticket context (key, summary, description, resources)
    linked_info: dict | None = None  # Linked issues (blocks, blocked_by, causes, caused_by)


class PostCommentRequest(BaseModel):
    """Request body for posting a comment to Jira."""

    issue_key: str
    comment_text: str
