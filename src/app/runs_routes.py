"""
Run history routes — read-only endpoints that surface prior test-plan runs for a
ticket so the UI can show a "this ticket has been analyzed before" banner and
diff regenerations against previous versions.

Mounted in main.py via APIRouter.
"""

import logging

from fastapi import APIRouter, HTTPException

from .db.session import get_sessionmaker
from .repositories import plan_repository

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/runs/by-ticket/{ticket_key}")
async def runs_by_ticket(ticket_key: str):
    """Return successful test-plan runs that touched this ticket, newest first.

    Lightweight payload (run + plan summary fields only). The frontend uses this
    to render a banner; full plan body is fetched lazily via /plans/{plan_id}.
    """
    key = ticket_key.upper()
    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            runs = await plan_repository.list_runs_with_plans_by_ticket(
                session, ticket_key=key
            )
    except Exception:
        logger.exception("runs_by_ticket failed for %s", key)
        raise HTTPException(status_code=503, detail="History unavailable")
    return {"ticket_key": key, "runs": runs}


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: int):
    """Return a stored plan with its body (JSON for test_plan format) and ordered
    test cases. Used by the View action and the diff modal."""
    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await plan_repository.get_plan_with_cases(
                session, plan_id=plan_id
            )
    except Exception:
        logger.exception("get_plan failed for %s", plan_id)
        raise HTTPException(status_code=503, detail="Plan store unavailable")

    if result is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    plan, cases = result
    return {
        "plan_id": plan.id,
        "run_id": plan.run_id,
        "format": plan.format.value,
        "body": plan.body,
        "version": plan.version,
        "previous_plan_id": plan.previous_plan_id,
        "case_count": plan.case_count,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "cases": [
            {
                "position": c.position,
                "title": c.title,
                "body": c.body,
                "category": c.category,
            }
            for c in cases
        ],
    }
