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
class JiraIssue:
    """Represents a Jira issue with extracted data."""

    key: str
    summary: str
    description: str | None
    description_analysis: DescriptionAnalysis
    labels: list[str]
    issue_type: str


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
