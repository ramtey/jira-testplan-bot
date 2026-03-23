"""
Bug Lens routes — analyze bug tickets to explain root cause, fix, and regression tests.

Mounted at /bug-lens in main.py via APIRouter.
"""

from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from .llm_client import LLMError, get_llm_client
from .models import BugAnalysisRequest, MultiBugAnalysisRequest

router = APIRouter(prefix="/bug-lens", tags=["bug-lens"])


@router.post("/analyze")
async def analyze_bug(request: BugAnalysisRequest):
    """
    Analyze a single bug ticket.

    Uses the configured LLM to explain the bug, identify root cause,
    describe the fix (if a merged PR exists), and suggest regression tests.
    """
    try:
        llm = get_llm_client()
        analysis = await llm.generate_bug_analysis(
            ticket_key=request.ticket_key,
            summary=request.summary,
            description=request.description,
            development_info=request.development_info,
            comments=request.comments,
            linked_info=request.linked_info,
        )
        return {
            "ticket_key": request.ticket_key,
            **asdict(analysis),
        }
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/analyze/multi")
async def analyze_bugs_multi(request: MultiBugAnalysisRequest):
    """
    Analyze multiple related bug tickets together.

    Produces a single combined analysis covering the shared root cause,
    fix explanation, and regression tests across all tickets.
    """
    tickets_data = [
        {
            "ticket_key": t.ticket_key,
            "summary": t.summary,
            "description": t.description,
            "development_info": t.development_info,
            "comments": t.comments,
            "linked_info": t.linked_info,
        }
        for t in request.tickets
    ]

    try:
        llm = get_llm_client()
        analysis = await llm.generate_multi_bug_analysis(tickets=tickets_data)
        return {
            "ticket_keys": [t.ticket_key for t in request.tickets],
            **asdict(analysis),
        }
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e))
