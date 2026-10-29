"""The one human checkpoint of a UAT run: review, overrides, Approve & post.

A run awaiting review is read here as the reviewer needs it (``review_view``).
The reviewer may record their own result for any case (``record_override``),
then approves or rejects. Approval is the only path to the tracker: agent
results start to count on the checklist, the walkthrough and ``FAIL-`` frames
are attached, the results comment is written (one per ticket, edited in place),
and the chosen workflow transition runs. Each of those steps is recorded on the
run's ``tracker``, so a partial post is a visible state and calling approve
again resumes it instead of repeating what already landed.

The comment text comes from the browser's formatter run under node, so the
preview in the review panel is the comment that is posted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..attachment_types import MAX_ATTACHMENT_BYTES
from ..db.base import utcnow
from ..db.models.case_result import CaseResult
from ..db.models.uat_run import UatRun
from ..jira_client import JiraClient
from ..repositories import case_result_repository, plan_repository, uat_run_repository
from .uat_runs import RunRefused, _live_own_plan, _record, latest_by_case, run_view

logger = logging.getLogger(__name__)

_FORMATTER = (
    Path(__file__).resolve().parents[3] / "frontend" / "scripts" / "format-uat-results.mjs"
)
_FORMAT_TIMEOUT_S = 30

# Not Run codes a rerun will not clear: a person has to do these.
NEEDS_PERSON_CODES = frozenset({
    "not_automatable", "needs_client_token", "not_independently_verified", "missing_fixture",
})
NEXT_STEPS = ("pass-to-uat", "fail-to-in-progress", "fail-to-todo")
OVERRIDE_STATUSES = ("passed", "failed", "not_run")


class ResultsFormatError(RuntimeError):
    """The results comment could not be rendered."""


def derive_next_step(counts: dict[str, int]) -> str | None:
    """The workflow action the results point to; None means stay in testing.

    Any failure sends the ticket back. Any case without a result that counts
    keeps it in testing, since someone still has to test it. Only an all-pass
    run moves it on.
    """
    if counts.get("failed"):
        return "fail-to-in-progress"
    if counts.get("not_run"):
        return None
    return "pass-to-uat" if counts.get("passed") else None


def _media(run: UatRun) -> dict:
    """The walkthrough video and failure frames in the run's evidence directory.

    The narrated cut is preferred. Files over the attachment cap are listed
    with ``too_large`` rather than dropped, so the review says why.
    """
    out: dict[str, Any] = {"video": None, "fail_frames": []}
    if not run.evidence_dir:
        return out
    root = Path(run.evidence_dir)
    if not root.is_dir():
        return out

    def entry(p: Path) -> dict:
        size = p.stat().st_size
        return {"name": p.name, "bytes": size, "too_large": size > MAX_ATTACHMENT_BYTES}

    videos = sorted(p for p in root.glob("*.mp4") if "walkthrough" in p.name)
    narrated = [p for p in videos if "narrated" in p.name]
    pick = (narrated or videos or [None])[-1]
    if pick is not None:
        out["video"] = entry(pick)
    out["fail_frames"] = [entry(p) for p in sorted(root.glob("*FAIL-*.png"))]
    return out


async def _plan_live(db, run: UatRun) -> dict:
    found = await _live_own_plan(db, run.ticket_key)
    if found is None:
        return {"live": False, "live_plan_id": None, "live_plan_version": None}
    _row, plan = found
    return {"live": plan.id == run.plan_id, "live_plan_id": plan.id,
            "live_plan_version": plan.version}


def _actor_view(actor: dict | None) -> dict:
    a = actor or {}
    return {"kind": a.get("kind"), "name": a.get("name"), "email": a.get("email")}


async def review_view(db: AsyncIOMotorDatabase, run: UatRun) -> dict:
    """The run as the review panel shows it, and as the formatter posts it."""
    out = await run_view(db, run)
    results = latest_by_case(await uat_run_repository.results_for_run(db, run.id))
    for case in out["cases"]:
        r = results.get(case["id"])
        if case["result"] is not None and r is not None:
            case["result"]["actor"] = _actor_view(r.actor)
            case["result"]["review_approved"] = r.review_approved
        case.pop("case", None)
    found = await plan_repository.get_plan_with_cases(db, plan_id=run.plan_id)
    body = None
    if found is not None and found[0].body:
        try:
            body = json.loads(found[0].body)
        except (ValueError, TypeError):
            body = None
    duration_s = None
    if run.created_at and run.finished_at:
        duration_s = round((run.finished_at - run.created_at).total_seconds())
    out.update({
        "plan": await _plan_live(db, run),
        "how_to_see_it": body.get("how_to_see_it") if isinstance(body, dict) else None,
        "cost_usd": run.cost_usd,
        "budget_usd": run.budget_usd,
        "duration_s": duration_s,
        "permission_denials": {n: l.get("permission_denials") or [] for n, l in run.legs.items()},
        "media": _media(run),
        "derived_next_step": derive_next_step(out["counts"]),
        "needs_person_codes": sorted(NEEDS_PERSON_CODES),
        "review": run.review,
        "tracker": run.tracker,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    })
    return out


async def record_override(db: AsyncIOMotorDatabase, run: UatRun, *, case_id: str,
                          status: str, note: str | None = None,
                          not_run_code: str | None = None) -> CaseResult:
    """The reviewer's own result for one case, recorded with them as actor.

    Allowed only while the run awaits review: after approval the posted
    comment would no longer match the results.
    """
    if run.status != "awaiting_review":
        raise RunRefused("not_awaiting_review", f"run {run.id} is {run.status}; overrides are closed")
    if case_id not in run.routing:
        raise RunRefused("unknown_case", f"{case_id} is not a case in run {run.id}", status=422)
    if status not in OVERRIDE_STATUSES:
        raise RunRefused("bad_status", f"status must be one of {', '.join(OVERRIDE_STATUSES)}",
                         status=422)
    note = (note or "").strip()
    if status == "failed" and not note:
        raise RunRefused("note_required", "say what failed", status=422)
    extra: dict[str, Any] = {}
    if note:
        extra["evidence"] = [{"kind": "assertion", "text": note}]
    if status == "not_run":
        from ..db.models.uat_run import NOT_RUN_CODES
        if not_run_code not in NOT_RUN_CODES or not note:
            raise RunRefused("reason_required", "a Not Run needs a known code and a note",
                             status=422)
        extra["not_run"] = {"code": not_run_code, "detail": note}
    actor = await case_result_repository.human_actor(db)
    return await _record(db, run, case_id=case_id, status=status, actor=actor,
                         source="review", **extra)


async def render_results_text(payload: dict) -> str:
    """The results comment, from the browser's own formatter."""
    node = shutil.which("node")
    if not node:
        raise ResultsFormatError("node is not on PATH, so the results cannot be formatted")
    proc = await asyncio.create_subprocess_exec(
        node, str(_FORMATTER),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(json.dumps(payload, default=str).encode()), _FORMAT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise ResultsFormatError(f"the formatter took longer than {_FORMAT_TIMEOUT_S}s")
    if proc.returncode != 0:
        raise ResultsFormatError(
            f"the formatter exited {proc.returncode}: {err.decode(errors='replace')[-500:]}")
    text = out.decode()
    if not text.strip():
        raise ResultsFormatError("the formatter produced an empty comment")
    return text


def _validate_next_step(next_step: str | None) -> None:
    if next_step is not None and next_step not in NEXT_STEPS:
        raise RunRefused("bad_next_step",
                         f"next step must be one of {', '.join(NEXT_STEPS)}, or none", status=422)


async def reject(db: AsyncIOMotorDatabase, run: UatRun, *, notes: str | None) -> UatRun:
    """Close the run without posting. Agent results never count."""
    if run.status != "awaiting_review":
        raise RunRefused("not_awaiting_review", f"run {run.id} is {run.status}")
    run.review = {"decision": "rejected", "actor": await case_result_repository.human_actor(db),
                  "at": utcnow().isoformat(), "next_step": None, "notes": (notes or "").strip() or None}
    run.status = "rejected"
    return await uat_run_repository.save(db, run)


async def approve(db: AsyncIOMotorDatabase, run: UatRun, *, next_step: str | None,
                  notes: str | None, jira: JiraClient | None = None,
                  transition=None) -> UatRun:
    """Approve the run and post it, or resume a post that stopped partway.

    Refused before anything is written when the run is not reviewable or its
    pinned plan is no longer the live one. The decision is recorded first; the
    tracker steps follow in order (attachments, comment, transition) and each
    one's outcome is saved as it happens. A failed step stops the sequence and
    names itself in ``tracker.outcome``; approving again resumes from it.
    """
    resuming = run.status == "approved" and run.tracker.get("outcome") != "posted"
    if run.status != "awaiting_review" and not resuming:
        raise RunRefused("not_awaiting_review", f"run {run.id} is {run.status}")
    if not resuming:
        _validate_next_step(next_step)
        plan = await _plan_live(db, run)
        if not plan["live"]:
            raise RunRefused(
                "stale_plan",
                f"run {run.id} tested plan v{run.plan_version}; the live plan is "
                f"{'v' + str(plan['live_plan_version']) if plan['live_plan_id'] else 'not posted'}. "
                "Rerun on the live plan instead of posting old results.")
        run.review = {"decision": "approved", "actor": await case_result_repository.human_actor(db),
                      "at": utcnow().isoformat(), "next_step": next_step,
                      "notes": (notes or "").strip() or None}
        run.status = "approved"
        run.tracker = {"outcome": "pending"}
        await uat_run_repository.save(db, run)
        # The agent's results now count on the checklist, with the run's
        # review as the record of who accepted them.
        await db[CaseResult.__collection__].update_many(
            {"run_id": run.id, "actor.kind": "agent"}, {"$set": {"review_approved": True}})
    return await _post(db, run, jira=jira or JiraClient(), transition=transition)


async def _post(db, run: UatRun, *, jira: JiraClient, transition) -> UatRun:
    tracker = dict(run.tracker or {})
    key = run.ticket_key

    async def fail(outcome: str, exc: Exception) -> UatRun:
        logger.warning("UAT run %s: %s on %s: %s", run.id, outcome, key, exc)
        tracker.update(outcome=outcome, error=f"{type(exc).__name__}: {getattr(exc, 'detail', None) or exc}")
        run.tracker = tracker
        return await uat_run_repository.save(db, run)

    # 1. Attachments. Names already uploaded by an earlier attempt are skipped.
    media = _media(run)
    done = {a["name"] for a in tracker.get("attachments") or []}
    files = [m for m in ([media["video"]] if media["video"] else []) + media["fail_frames"]
             if not m["too_large"] and m["name"] not in done]
    if files:
        try:
            root = Path(run.evidence_dir)
            payload = [(m["name"], (root / m["name"]).read_bytes(),
                        "video/mp4" if m["name"].endswith(".mp4") else "image/png") for m in files]
            uploaded = await jira.upload_attachments(key, payload)
        except Exception as exc:
            return await fail("attachments_failed", exc)
        tracker["attachments"] = list(tracker.get("attachments") or []) + [
            {"name": m["name"], "id": str(u.get("id"))} for m, u in zip(files, uploaded)]
        run.tracker = tracker
        await uat_run_repository.save(db, run)

    # 2. The results comment.
    try:
        view = await review_view(db, run)
        text = await render_results_text(view)
        written = await jira.upsert_uat_results_comment(key, text, tracker.get("comment_id"))
    except Exception as exc:
        return await fail("comment_failed", exc)
    tracker.update(comment_id=written["id"], comment_updated=written["updated"],
                   posted_at=utcnow().isoformat(), error=None)
    run.tracker = tracker
    await uat_run_repository.save(db, run)

    # 3. The transition, once.
    action = (run.review or {}).get("next_step")
    if action and not (tracker.get("transition") or {}).get("done"):
        if transition is None:
            from ..workflow_routes import transition_and_assign as transition
        try:
            moved = await transition(jira, key, action)
        except Exception as exc:
            return await fail("transition_failed", exc)
        tracker["transition"] = {**moved, "done": True}
    tracker.update(outcome="posted", error=None)
    run.tracker = tracker
    return await uat_run_repository.save(db, run)
