from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field

from src.app.db.base import DocumentBase, utcnow


class JiraTicket(DocumentBase):
    __collection__: ClassVar[str] = "jira_tickets"

    ticket_key: str
    project_key: str
    issue_type: str | None = None
    status: str | None = None
    title: str | None = None
    parent_key: str | None = None
    last_seen_at: datetime = Field(default_factory=utcnow)
    # Set the first time the "pull to In Testing" workflow auto-dispatched a
    # Bug Lens run for this ticket. Persists across aborted plan generations so
    # we never auto-fire the analysis twice.
    auto_bug_analysis_dispatched_at: datetime | None = None
