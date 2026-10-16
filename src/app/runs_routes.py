"""
Run history routes — read-only endpoints that surface prior test-plan runs for a
ticket so the UI can show a "this ticket has been analyzed before" banner and
diff regenerations against previous versions.

Mounted in main.py via APIRouter.
"""

import logging

from fastapi import APIRouter, HTTPException

from .config import settings
from .db.mongo import get_db
from .repositories import auto_post_repository, jira_ticket_repository, plan_repository
from .services import progress_key as progress_key_service

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
        db = get_db()
        runs = await plan_repository.list_runs_with_plans_by_ticket(
            db, ticket_key=key
        )
        auto_dispatched_at = (
            await jira_ticket_repository.get_auto_bug_analysis_dispatched_at(
                db, ticket_key=key
            )
        )
    except Exception:
        logger.exception("runs_by_ticket failed for %s", key)
        raise HTTPException(status_code=503, detail="History unavailable")
    return {
        "ticket_key": key,
        "runs": runs,
        "post_state": await _post_state(db, key),
        "auto_bug_analysis_dispatched_at": (
            auto_dispatched_at.isoformat() if auto_dispatched_at else None
        ),
    }


async def _post_state(db, key: str) -> dict | None:
    """What the plan view needs to word its Post control.

    The live own-plan comes from `list_live_plans_for_ticket`, not from the
    runs list above: that list stops at 20 rows and the live plan can sit past
    them. None means "could not look", which the view must not read as "not
    live" — it falls back to the plain Post button instead.
    """
    try:
        live = await plan_repository.list_live_plans_for_ticket(db, ticket_key=key)
        last = await auto_post_repository.get_last(db, ticket_key=key)
    except Exception:
        logger.exception("post state unavailable for %s", key)
        return None
    own = next(
        ((run, plan) for run, plan in live if len(run.ticket_keys or []) == 1), None
    )
    return {
        "live_own_plan": (
            {
                "plan_id": own[1].id,
                "version": own[1].version,
                # So the view can tell whether the last auto-post outcome is
                # about the post that is live now or one a re-post replaced.
                "posted_at": own[1].posted_at.isoformat() if own[1].posted_at else None,
            }
            if own
            else None
        ),
        "last_auto_post": last,
        # Where in the workflow auto-post fires, so the view can promise it
        # only on a ticket that is actually headed there.
        "auto_post_status": settings.auto_post_status or None,
        "queue_status": settings.watch_status or None,
    }


@router.get("/tickets/{ticket_key}/live-plan")
async def live_plan(ticket_key: str):
    """The plan(s) live in Jira for this ticket, every case under its canonical id.

    The UAT runner's source of truth. It used to parse the Jira comment, which
    is a rendering: a large plan's comment is only an index, and pairing cases
    with ids by reading the comment is what marked the wrong case on SK-2325.

    `live_plans` is empty when nothing has been posted, and holds two entries
    when the ticket has both its own plan and a batch plan covering it; which
    one to run is the tester's call. A store failure is a 503, never an empty
    list, so "no plan" and "could not look" cannot be confused.
    """
    key = ticket_key.upper()
    try:
        db = get_db()
        live = await plan_repository.list_live_plans_for_ticket(db, ticket_key=key)
    except Exception:
        logger.exception("live_plan failed for %s", key)
        raise HTTPException(status_code=503, detail="Plan store unavailable")

    return {
        "ticket_key": key,
        "live_plans": [
            {
                "plan_id": plan.id,
                "version": plan.version,
                "ticket_keys": list(run.ticket_keys or []),
                "batch": len(run.ticket_keys or []) > 1,
                "jira_comment_id": plan.jira_comment_id,
                "posted_at": plan.posted_at.isoformat() if plan.posted_at else None,
                "progress_key": progress_key_service.build_progress_key(
                    list(run.ticket_keys or []), plan.body
                ),
                "cases": progress_key_service.cases_by_id(plan.body),
            }
            for run, plan in live
        ],
    }


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: int):
    """Return a stored plan with its body (JSON for test_plan format) and ordered
    test cases. Used by the View action and the diff modal."""
    try:
        db = get_db()
        result = await plan_repository.get_plan_with_cases(
            db, plan_id=plan_id
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
        "jira_comment_id": plan.jira_comment_id,
        "posted_at": plan.posted_at.isoformat() if plan.posted_at else None,
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
