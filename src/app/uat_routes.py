"""UAT run routes: how a runner (through ``src/uat_mcp``) and the CLI reach runs.

The runner never calls the tracker. Everything it writes lands here. The only
route that posts is ``approve``, which a person calls from the review panel;
the runner's MCP server has no tool that reaches the review routes.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .db.mongo import get_db
from .repositories import uat_run_repository
from .services import uat_review, uat_runs
from .services.uat_runs import RunRefused

logger = logging.getLogger(__name__)

router = APIRouter()


class StartRunRequest(BaseModel):
    ticket_key: str
    trigger: str = "command"
    dry_run: bool = False
    budget_usd: float | None = None


class RunnerResultRequest(BaseModel):
    case_id: str
    status: str
    not_run: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = []
    client_id: str | None = None
    flaky: bool = False
    attempts: int = 1
    started_at: str | None = None
    finished_at: str | None = None


class LegInfo(BaseModel):
    pid: int | None = None
    host: str | None = None
    model: str | None = None
    session_id: str | None = None
    exit_code: int | None = None
    detail: str | None = None
    cost_usd: float | None = None
    duration_s: float | None = None
    permission_denials: list[dict[str, Any]] = []
    budget_alert: str | None = None
    # The Not Run code for cases the runner never reached, when the CLI knows
    # why (dropped_time_cap after the wall-clock stop). Default run_aborted.
    missing_code: str | None = None


class CancelRequest(BaseModel):
    reason: str = "cancelled by a person"


class OverrideRequest(BaseModel):
    case_id: str
    status: str
    note: str | None = None
    not_run_code: str | None = None


class ReviewDecision(BaseModel):
    # A workflow action (pass-to-uat, fail-to-in-progress, fail-to-todo), or
    # None to leave the ticket where it is.
    next_step: str | None = None
    notes: str | None = None


def _refused(e: RunRefused) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"error": e.code, "message": e.message, **e.extra})


async def _run(db, run_id: int):
    run = await uat_run_repository.get(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"error": "run_not_found", "message": f"no run {run_id}"})
    return run


@router.post("/uat-runs")
async def start_run(request: StartRunRequest):
    """Pin a run to the ticket's live plan and route its cases to legs."""
    db = get_db()
    try:
        run = await uat_runs.start_run(db, ticket_key=request.ticket_key, trigger=request.trigger,
                                       dry_run=request.dry_run, budget_usd=request.budget_usd)
    except RunRefused as e:
        raise _refused(e)
    return await uat_runs.run_view(db, run)


@router.get("/uat-runs/{run_id}")
async def get_run(run_id: int, leg: str | None = None):
    db = get_db()
    run = await _run(db, run_id)
    if leg is not None and leg not in run.legs:
        raise HTTPException(status_code=422, detail={"error": "unknown_leg", "message": f"no leg {leg!r}"})
    return await uat_runs.run_view(db, run, leg=leg)


@router.get("/tickets/{ticket_key}/uat-runs")
async def runs_for_ticket(ticket_key: str):
    db = get_db()
    runs = await uat_run_repository.list_for_ticket(db, ticket_key=ticket_key)
    return {"ticket_key": ticket_key.upper(), "runs": [
        {"run_id": r.id, "status": r.status, "status_reason": r.status_reason,
         "plan_id": r.plan_id, "plan_version": r.plan_version, "counts": r.counts,
         "cost_usd": r.cost_usd, "dry_run": r.dry_run,
         "created_at": r.created_at.isoformat() if r.created_at else None,
         "finished_at": r.finished_at.isoformat() if r.finished_at else None,
         "tracker_outcome": (r.tracker or {}).get("outcome")}
        for r in runs]}


@router.get("/uat-runs/{run_id}/review")
async def get_review(run_id: int):
    """The run as the review panel shows it; also the formatter's input."""
    db = get_db()
    return await uat_review.review_view(db, await _run(db, run_id))


@router.post("/uat-runs/{run_id}/review/results")
async def record_override(run_id: int, request: OverrideRequest):
    """The reviewer's own result for one case, recorded with them as actor."""
    db = get_db()
    run = await _run(db, run_id)
    try:
        await uat_review.record_override(db, run, case_id=request.case_id, status=request.status,
                                         note=request.note, not_run_code=request.not_run_code)
    except RunRefused as e:
        raise _refused(e)
    return await uat_review.review_view(db, run)


@router.post("/uat-runs/{run_id}/approve")
async def approve(run_id: int, request: ReviewDecision):
    """Approve & post. Calling it again resumes a post that stopped partway.

    Returns 200 with ``tracker.outcome`` naming the step that failed, rather
    than an error, once the approval itself is recorded: the decision stands
    and the panel offers a retry.
    """
    db = get_db()
    run = await _run(db, run_id)
    try:
        run = await uat_review.approve(db, run, next_step=request.next_step, notes=request.notes)
    except RunRefused as e:
        raise _refused(e)
    return await uat_review.review_view(db, run)


@router.post("/uat-runs/{run_id}/reject")
async def reject(run_id: int, request: ReviewDecision):
    """Close the run without posting; its agent results never count."""
    db = get_db()
    run = await _run(db, run_id)
    try:
        run = await uat_review.reject(db, run, notes=request.notes)
    except RunRefused as e:
        raise _refused(e)
    return await uat_review.review_view(db, run)


@router.post("/uat-runs/{run_id}/legs/{leg}/results")
async def record_result(run_id: int, leg: str, request: RunnerResultRequest):
    """One runner result. A pass or failure without evidence, or a Not Run
    without a reason code and detail, is a 422 and nothing is written."""
    db = get_db()
    run = await _run(db, run_id)
    try:
        return await uat_runs.record_result(db, run, leg=leg, payload=request.model_dump())
    except RunRefused as e:
        raise _refused(e)


@router.get("/uat-runs/{run_id}/continue")
async def should_continue(run_id: int):
    db = get_db()
    run = await _run(db, run_id)
    return await uat_runs.should_continue(db, run)


@router.post("/uat-runs/{run_id}/cancel")
async def cancel(run_id: int, request: CancelRequest):
    db = get_db()
    run = await _run(db, run_id)
    run = await uat_runs.cancel(db, run, reason=request.reason)
    return {"run_id": run.id, "status": run.status, "status_reason": run.status_reason}


@router.post("/uat-runs/{run_id}/legs/{leg}/start")
async def start_leg(run_id: int, leg: str, request: LegInfo):
    db = get_db()
    run = await _run(db, run_id)
    try:
        await uat_runs.start_leg(db, run, leg, request.model_dump())
    except RunRefused as e:
        raise _refused(e)
    return {"run_id": run_id, "leg": leg, "status": "running"}


@router.post("/uat-runs/{run_id}/legs/{leg}/finalize")
async def finalize_leg(run_id: int, leg: str, request: LegInfo):
    """Close a leg after its runner exits. Every case it owed gets a result."""
    db = get_db()
    run = await _run(db, run_id)
    try:
        run = await uat_runs.finalize_leg(db, run, leg, request.model_dump())
    except RunRefused as e:
        raise _refused(e)
    return await uat_runs.run_view(db, run)
