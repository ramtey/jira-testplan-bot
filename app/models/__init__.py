from pydantic import BaseModel
from typing import List


class TestCase(BaseModel):
    """Model for a single test case."""
    scenario: str
    steps: List[str]
    expected_result: str


class TestPlan(BaseModel):
    """Model for a complete test plan."""
    happy_path: List[TestCase]
    edge_cases: List[TestCase]
    regression_checklist: List[str]
    questions: List[str]


class GenerateRequest(BaseModel):
    """Request model for generate endpoint."""
    issue_key: str


class HealthResponse(BaseModel):
    """Response model for health endpoint."""
    status: str
    message: str
