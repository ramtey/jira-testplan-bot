"""The PR state re-check every post goes through (src/app/pr_state_recheck.py).

The Post button and the batch post send text the browser rendered from the
plan it holds; auto-post renders it here. Either way, before anything reaches
Jira the stored plan's grounding PRs are re-read. A plan whose grounding moved
is refused with the reason. A plan whose PRs only changed state is updated in
place — stored under the same id and version — and re-rendered by the same
formatter the button uses, so the comment carries the refreshed plan rather
than the text the browser built from the stale one.
"""

from __future__ import annotations

import json
import logging

from src.app.db.mongo import get_db
from src.app.pr_state_recheck import PHASE_POST, recheck_pr_states
from src.app.repositories import plan_repository

logger = logging.getLogger(__name__)


class PlanGroundingStale(RuntimeError):
    """The plan's grounding PRs moved since it was written; it must not be posted."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("Not posted: " + "; ".join(reasons) + ".")


async def recheck_stored_plan(plan_id: int, *, github=None) -> tuple[dict | None, dict | None]:
    """``(plan, report)`` for stored plan ``plan_id`` after the re-check.

    ``plan`` is the body with ``plan_id``/``version``/``created_at`` added, as
    the formatter expects; it is saved back when the re-check changed it.
    Raises PlanGroundingStale when the grounding is out of date. ``(None,
    None)`` when there is no stored plan to check.
    """
    db = get_db()
    found = await plan_repository.get_plan_with_cases(db, plan_id=plan_id)
    if found is None:
        return None, None
    stored, _cases = found
    body = json.loads(stored.body)
    report = await recheck_pr_states(body, phase=PHASE_POST, github=github, created_at=stored.created_at)
    if report["refuse"]:
        raise PlanGroundingStale(report["refuse"])
    if report["modified"]:
        await plan_repository.update_plan_body(db, plan_id=plan_id, body=json.dumps(body))
    plan = {
        **body,
        "plan_id": stored.id,
        "version": stored.version,
        "created_at": stored.created_at.isoformat() if stored.created_at else None,
    }
    return plan, report


async def comment_text_for_post(
    plan_id: int | None, comment_text: str, *, walkthrough_key: str, github=None
) -> tuple[str, dict | None]:
    """The text to post for a manual post: the browser's, or a re-render after a refresh."""
    if plan_id is None:
        return comment_text, None
    try:
        plan, report = await recheck_stored_plan(plan_id, github=github)
    except PlanGroundingStale:
        raise
    except Exception as exc:
        # The store or GitHub failed under the re-check itself. The post path
        # never costs the tester their post over a database outage, so it goes
        # up as the browser rendered it, but the response says the re-check did
        # not run: unchecked is not the same as checked and unchanged.
        logger.exception("PR state re-check before posting plan %s failed", plan_id)
        return comment_text, {"error": f"{type(exc).__name__}: {exc}"}
    if plan is None or not report or not report.get("modified"):
        return comment_text, report
    # Imported here: auto_post imports plan_posting, which main also imports.
    from src.app import uat_readiness
    from src.app.services.auto_post import render_jira_text

    walkthrough = await uat_readiness.fetch_readiness(get_db(), ticket_key=walkthrough_key)
    return await render_jira_text(plan, walkthrough), report


def describe(report: dict | None) -> dict | None:
    """What the post response carries about the re-check, for the browser to show."""
    if not report:
        return None
    if report.get("error"):
        return {"modified": False, "error": report["error"]}
    return {
        "modified": report["modified"],
        "changes": report["changes"],
        "blockers_dropped": len(report["blockers_dropped"]),
        "blockers_unverified": len(report["blockers_unverified"]),
        "unreadable": report["unreadable"],
    }
