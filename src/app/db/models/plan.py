from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import ClassVar

from src.app.db.base import DocumentBase


class PlanFormat(str, Enum):
    markdown = "markdown"
    jira = "jira"
    json = "json"


class GeneratedPlan(DocumentBase):
    __collection__: ClassVar[str] = "generated_plans"

    run_id: int

    format: PlanFormat
    body: str
    case_count: int = 0
    version: int = 1
    previous_plan_id: int | None = None

    # Set when this version is the one currently live on the Jira ticket.
    # Because posting is update-in-place, at most one plan per ticket has
    # these populated at a time — posting a newer version nulls them on
    # superseded rows.
    jira_comment_id: str | None = None
    posted_at: datetime | None = None


class PlanTestCase(DocumentBase):
    __collection__: ClassVar[str] = "plan_test_cases"

    plan_id: int
    position: int = 0
    title: str
    body: str
    category: str | None = None
