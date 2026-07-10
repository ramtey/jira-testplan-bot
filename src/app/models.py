"""
Data models for the Jira Test Plan Bot.

This module contains all Pydantic and dataclass models used throughout the application.
"""

from dataclasses import dataclass

from pydantic import BaseModel, Field


# ============================================================================
# Jira-related models
# ============================================================================


@dataclass
class DescriptionAnalysis:
    """Concrete gaps a QA reader would have to chase down before testing."""

    has_description: bool
    gaps: list[str]
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
    author: str | None = None  # PR author (GitHub login or Jira display name)
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
    # Contents of a few existing unit/spec files so the planner can flag test
    # cases that are already covered by automated tests. Each entry:
    # {"path": "src/foo.test.ts", "content": "<source, truncated>"}.
    unit_test_sources: list[dict] | None = None
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
    author_account_id: str | None = None  # For ADF mention nodes


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
class ChildIssue:
    """A direct child (sub-task / story under an Epic) of the current ticket.

    Lightweight by design: enough for the LLM to understand each child's
    scope when writing an integration-focused parent test plan, without
    pulling per-child PR diffs or attachments (those live with the child's
    own test plan).
    """

    key: str
    summary: str
    description: str | None
    issue_type: str
    status: str | None = None
    status_category: str | None = None
    # Acceptance criteria parsed out of the child's description. Surfaced
    # structurally so the parent test plan can enumerate every per-subtask AC
    # as a discrete test, rather than relying on a truncated description blob
    # that may have cut the enumerated entry-point / surface list.
    acceptance_criteria: list[str] | None = None


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
class EpicChildSummary:
    """Lightweight summary of a child ticket under an Epic."""

    key: str
    summary: str
    issue_type: str
    status: str | None = None
    status_category: str | None = None
    parent_key: str | None = None
    # True if the issue is in an active sprint, False if it has sprint history
    # but no active sprint, None if the project doesn't use sprints at all.
    in_active_sprint: bool | None = None


@dataclass
class SlackMessage:
    """A single Slack message resolved from a permalink in a Jira ticket."""

    url: str
    channel_id: str
    ts: str
    author: str | None
    text: str
    thread_ts: str | None = None


@dataclass
class BounceEvent:
    """A backward status transition (e.g. UAT/QA → To Do) detected in the ticket's changelog.

    These flag regression-prone moments — the ticket reached an advanced workflow state
    and was then sent back, usually because something didn't work. The `reason` is the
    body of the Jira comment closest in time to the transition (best-effort).
    """

    from_status: str
    to_status: str
    timestamp: str  # ISO-8601 from Jira changelog
    author: str | None = None  # Who performed the transition
    reason: str | None = None  # Nearest-in-time comment body, truncated


@dataclass
class JiraIssue:
    """Represents a Jira issue with extracted data."""

    key: str
    summary: str
    description: str | None
    description_analysis: DescriptionAnalysis
    labels: list[str]
    issue_type: str
    assignee: str | None = None  # Current Jira assignee display name
    assignee_account_id: str | None = None  # For ADF mention nodes
    assignee_history: list[str] | None = None  # All unique people ever assigned (from changelog)
    # Parallel to assignee_history; same length, same order. None when the
    # changelog item didn't carry an accountId (e.g. anonymized older changes).
    assignee_history_account_ids: list[str | None] | None = None
    development_info: DevelopmentInfo | None = None
    attachments: list[Attachment] | None = None
    comments: list[JiraComment] | None = None  # Filtered testing-related comments
    parent: ParentIssue | None = None  # Parent ticket context with design resources
    # Direct children (sub-tasks / Epic children). Populated for parent tickets
    # so the test-plan prompt can write integration-focused coverage rather than
    # treating the ticket as a leaf.
    children: list[ChildIssue] | None = None
    linked_issues: LinkedIssues | None = None  # Linked tickets (blocks, blocked by, etc.)
    status: str | None = None  # Workflow status name (e.g. "To Do", "In Progress", "In Testing", "Done")
    status_category: str | None = None  # Stable category key: "new" | "indeterminate" | "done"
    bounce_history: list[BounceEvent] | None = None  # Detected QA/UAT → ToDo regressions


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
    # Multi-ticket only: ACs from older tickets that were overridden by a newer
    # ticket's AC about the same observable behaviour. Each entry:
    # {"loser_id": "SK-2138-AC3", "winner_id": "SK-2194-AC1", "reason": "..."}
    superseded_acs: list[dict] | None = None
    # UI elements the LLM referenced in test steps but couldn't verify against
    # the PR diff or testID reference. Lets the reviewer spot tests that lean
    # on the AC's English description even though the actual code doesn't add
    # the named button/modal/field. Each entry:
    # {"ac_id": "SK-2138-AC5", "missing_element": "Edit button on popover",
    #  "explanation": "Searched the bulk-fill diff and testID reference; no
    #  control matching 'Edit' was found."}
    grounding_warnings: list[dict] | None = None
    # Cross-project mode only. Echoes the seam catalog the LLM was given so
    # the UI can render the producer/consumer pairs the plan covers. Shape:
    # {"verified_seams": [...], "suspected_seams": [...], "repos": [...]}.
    cross_project_summary: dict | None = None
    # How hard this ticket is for a UAT tester to even get to the point of
    # testing it: "low" | "medium" | "high". Drives whether the UI surfaces a
    # prominent "needs a walkthrough" banner and nudges the planner to attach a
    # Loom/screenshot before posting. Grounded in cross-project seams, grounding
    # warnings, linked-issue count, and manual-verification flags.
    uat_complexity: str | None = None
    # One short, non-technical sentence (or two) for a UAT tester who skims and
    # won't read the full plan: what observably changed + where to click to see
    # it. Shape: {"reason": "why it's complex", "summary": "plain how-to-see-it"}.
    how_to_see_it: dict | None = None


# ============================================================================
# Bug Lens models
# ============================================================================


@dataclass
class BugAnalysis:
    """Structured bug analysis output from LLM (Jira Bug Lens)."""

    bug_summary: str                # Plain-English explanation of what the bug is
    root_cause: str | None          # What in the code caused it
    fix_status: str                 # "not_fixed" | "in_testing" | "fixed"
    fix_explanation: str | None     # What the fix did (from diff); None if not yet fixed
    regression_tests: list[str]     # Concrete test cases to prevent this bug recurring
    similar_patterns: list[str]     # Classes of similar bugs to watch for
    fix_complexity: str | None      # "trivial" | "moderate" | "complex" | "architectural"; None if fixed
    fix_effort_estimate: str | None # e.g. "2–4 hours", "1–2 days"; None if fixed
    fix_complexity_reasoning: str | None  # Why this complexity level was chosen; None if fixed
    affected_flow: list[str] | None = None  # Numbered steps tracing the end-to-end path to the bug
    scope_of_impact: list[str] | None = None  # Other features/callers broken by the same issue
    why_tests_miss: str | None = None  # Why existing tests don't catch this bug
    is_regression: bool | None = None  # True if bug was previously working and then broke
    regression_introduced_by: str | None = None  # PR or commit that introduced the regression
    assumptions: list[str] | None = None  # Inferences the model made that aren't directly grounded in evidence
    open_questions: list[str] | None = None  # Ambiguities a human should resolve before estimating/fixing
    suspect_symbols: list[str] | None = None  # Symbols (component/function/class names) the LLM flagged for code search
    code_evidence: list[dict] | None = None  # Deterministic grep results for suspect_symbols — each entry: {suspect, repo, usages: [{path, ref, snippet}], notes}


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
    # Direct subtasks/Epic children of this ticket. When present, the prompt
    # treats this ticket as a parent and writes integration-focused coverage.
    child_info: list[dict] | None = None
    linked_info: dict | None = None  # Linked issues (blocks, blocked_by, causes, caused_by)
    bounce_history: list[dict] | None = None  # Prior QA/UAT bounce-back events with reasons


class TicketInput(BaseModel):
    """Individual ticket data for multi-ticket test plan generation."""

    ticket_key: str
    summary: str
    description: str | None = None
    issue_type: str
    testing_context: dict = {}
    development_info: dict | None = None
    image_urls: list[str] | None = None
    comments: list[dict] | None = None
    parent_info: dict | None = None
    child_info: list[dict] | None = None
    linked_info: dict | None = None
    bounce_history: list[dict] | None = None


class MultiTicketGenerateRequest(BaseModel):
    """Request body for generating a unified test plan from multiple related tickets."""

    tickets: list[TicketInput]


class PostCommentRequest(BaseModel):
    """Request body for posting a comment to Jira."""

    issue_key: str
    comment_text: str
    # When the comment is a generated test plan, the frontend passes the plan's
    # DB id so the server can record this version as the one currently live in
    # Jira and clear that mark from superseded versions of the same ticket.
    plan_id: int | None = None


class WalkthroughScreenshotRef(BaseModel):
    """A single already-uploaded walkthrough screenshot the client wants to
    keep. Carried in :class:`WalkthroughUpdateRequest.existing_screenshots`.
    ``url`` is the Jira content URL returned by the attachment endpoint;
    ``filename`` is the original file name (used to render the 📷 link)."""

    url: str
    filename: str | None = None


class WalkthroughUpdateRequest(BaseModel):
    """JSON payload part of the multipart walkthrough save.

    New screenshots are attached as multipart ``screenshots[]`` files (each
    uploaded to Jira on save). The JSON carries the text fields plus
    ``existing_screenshots`` — the subset of previously-uploaded screenshots
    the client wants to keep. The server writes the walkthrough's stored
    list as ``existing_screenshots ++ newly_uploaded``, so anything the
    client omitted is dropped from the walkthrough (the Jira attachment
    itself stays on the ticket).
    """

    loom_url: str | None = None
    notes: str | None = None
    existing_screenshots: list[WalkthroughScreenshotRef] = Field(default_factory=list)


class TestPlanProgressUpdateRequest(BaseModel):
    """Request body for saving a ticket's shared test-plan progress: the full set
    of checked test-case ids (e.g. ["happy_path:0", "edge_cases:2"]). The client
    sends the complete set each save, so the stored value is replaced wholesale."""

    checked_ids: list[str] = []


class WorkflowActionRequest(BaseModel):
    """Optional payload for /issue/{key}/workflow/{action}.

    `pass-to-uat` reads loom_urls + summary + environments; `fail-to-todo`
    reads reason (required for the comment to be posted) + loom_urls.
    All fields are optional at the schema level; the per-action handlers
    decide what's needed before posting. Image attachments are uploaded
    as multipart `images[]` files alongside this JSON payload — the
    endpoint uploads them to Jira and inlines the resulting attachment
    IDs in the comment.
    """

    loom_urls: list[str] | None = None
    summary: str | None = None
    environments: list[str] | None = None
    reason: str | None = None
    # Optional list of Jira accountIds to @mention in the comment body.
    mention_account_ids: list[str] | None = None
    # When true, after the primary issue transitions, cascade the same
    # transition (matched by target status name) to every direct subtask.
    # Subtasks whose workflow has no matching transition are skipped silently.
    cascade_to_subtasks: bool = False


class BugAnalysisRequest(BaseModel):
    """Request body for analyzing a single bug ticket (Jira Bug Lens)."""

    ticket_key: str
    summary: str
    description: str | None = None
    issue_type: str
    development_info: dict | None = None
    comments: list[dict] | None = None
    parent_info: dict | None = None
    linked_info: dict | None = None
    status: str | None = None
    status_category: str | None = None


class MultiBugAnalysisRequest(BaseModel):
    """Request body for analyzing multiple bug tickets together (Jira Bug Lens)."""

    tickets: list[BugAnalysisRequest]
