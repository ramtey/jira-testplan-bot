from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import Field

from src.app.db.base import DocumentBase

# Where a run is. None of these means "done" without saying how it ended.
UatRunStatus = Literal[
    "running",          # at least one leg may still record results
    "awaiting_review",  # every leg finished; a person decides next
    "cancelled",        # stopped on purpose (status_reason says by what)
    "failed",           # no leg could run at all (status_reason says why)
    "approved",
    "rejected",
]

ACTIVE_STATUSES = ("running",)

# Not Run reason codes. A Not Run without one of these is refused.
NOT_RUN_CODES = frozenset({
    "missing_fixture", "blocked_environment", "wrong_build", "needs_client_token",
    "deferred_to_ci", "not_independently_verified", "not_automatable",
    "dropped_time_cap", "permission_denied", "run_cancelled", "run_aborted",
    "plan_changed", "waiting_for_build", "no_runner_profile", "pr_not_merged",
})


class UatRun(DocumentBase):
    """One ticket's run of its live plan: one review, one tracker comment.

    Pinned at creation to the plan that was live (``plan_id``, ``plan_version``,
    ``progress_key``); a later regeneration does not move it. Split into legs,
    one per runner profile the cases need, because a runner only works from the
    directory where its app's tools are configured. Every case is routed to a
    leg or given a reason it will not run (``routing``).
    """

    __collection__: ClassVar[str] = "uat_runs"

    ticket_key: str
    plan_id: int
    plan_version: int
    progress_key: str
    issue_type: str | None = None
    trigger: str = "command"
    status: UatRunStatus = "running"
    status_reason: str | None = None
    status_detail: str | None = None
    # Set to ticket_key while the run is active, unset otherwise: the partial
    # unique index on it is what allows one active run per ticket.
    active_ticket: str | None = None
    dry_run: bool = False
    evidence_dir: str | None = None
    # {profile: {repos, surfaces, agent, cwd, status, case_ids, started_at,
    #  finished_at, exit_code, cost_usd, permission_denials, session_id, detail}}
    legs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # [{repo, merge_commit, pr_numbers, status: included|not_included|unverifiable|
    #   pr_not_merged, source: deploy_state|unverified, env, deployed_commit, detail}]
    builds: list[dict[str, Any]] = Field(default_factory=list)
    # {case_id: {"leg": profile | None, "repos": [...], "not_run": {code, detail} | None}}
    routing: dict[str, dict[str, Any]] = Field(default_factory=dict)
    ticket_snapshot: dict[str, Any] = Field(default_factory=dict)
    budget_usd: float | None = None
    cost_usd: float = 0.0
    counts: dict[str, int] = Field(default_factory=dict)
    finished_at: datetime | None = None
