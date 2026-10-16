"""Post a ticket's plan to Jira when the ticket enters testing.

Two triggers share this module. Pull to testing calls it right after the
transition; the watcher sweep calls it for tickets dragged into the column on
the board, which nothing here sees happen. Both go through `auto_post_own_plan`
so they skip the same tickets for the same reasons.

What it posts is deliberately narrow: the ticket's own single-ticket plan, and
only onto a ticket that has no plan comment yet. Everything else is left to a
person:

* **A plan already live, in Jira or in the store** — someone posted it, or
  chose which version to post. Replacing it would undo that choice, and an
  unposted regeneration sitting behind a live plan is exactly such a choice.
* **No plan of its own** — still generating, never generated, or covered only
  by a batch plan, whose host is the tester's call.
* **A plan with no cases** — this repo's recurring failure is a transient
  error saved as a plan (an empty tool_use, an off-VPN run). Posting it would
  put that failure on the ticket looking like a finished plan.
* **A plan older than the merged code** — the watcher never regenerates, so a
  PR merged after the plan leaves it describing code that moved. In the app
  StalePlanNotice says so; posted unattended, the comment would not. A merge
  time that cannot be read is treated the same: unconfirmed is not fresh.

Every outcome is returned with its reason, never collapsed into "done".
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.app.db.mongo import get_db
from src.app.jira_client import JiraClient, PlanCommentKind, _comment_marker_kind
from src.app.repositories import auto_post_repository, plan_repository
from src.app.services import plan_posting, plan_service
from src.app.source_grounding import PR_STATE_MERGED, classify_pr_state
from src.app import uat_readiness

logger = logging.getLogger(__name__)

POSTED = "posted"
SKIPPED = "skipped"
FAILED = "failed"

SKIP_NO_OWN_PLAN = "no_own_plan"
SKIP_EMPTY_PLAN = "empty_plan"
SKIP_ALREADY_LIVE = "already_live"
SKIP_COMMENT_EXISTS = "plan_comment_exists"
SKIP_STALE_PLAN = "stale_plan"
FAIL_FRESHNESS_UNKNOWN = "freshness_unknown"

_FORMATTER = (
    Path(__file__).resolve().parents[3] / "frontend" / "scripts" / "format-plan-jira.mjs"
)
_FORMAT_TIMEOUT_S = 30


@dataclass
class AutoPostOutcome:
    ticket_key: str
    action: str  # POSTED | SKIPPED | FAILED
    reason: str | None = None
    detail: str | None = None
    plan_id: int | None = None

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "detail": self.detail,
            "plan_id": self.plan_id,
        }


class PlanFormatError(RuntimeError):
    """The plan could not be rendered as its Jira comment."""


async def render_jira_text(plan: dict, walkthrough: dict | None) -> str:
    """The comment text for `plan`, from the browser's own formatter.

    Runs `formatTestPlanAsJira` under node instead of reimplementing it, so
    the automatic post and the Post to Jira button cannot disagree.
    """
    node = shutil.which("node")
    if not node:
        raise PlanFormatError("node is not on PATH, so the plan cannot be formatted")
    proc = await asyncio.create_subprocess_exec(
        node,
        str(_FORMATTER),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    payload = json.dumps({"plan": plan, "walkthrough": walkthrough}).encode()
    try:
        out, err = await asyncio.wait_for(proc.communicate(payload), _FORMAT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise PlanFormatError(f"the formatter took longer than {_FORMAT_TIMEOUT_S}s")
    if proc.returncode != 0:
        raise PlanFormatError(
            f"the formatter exited {proc.returncode}: {err.decode(errors='replace')[-500:]}"
        )
    text = out.decode()
    if not text.strip():
        raise PlanFormatError("the formatter produced an empty comment")
    return text


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, str) and value:
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def freshness_problem(serialized: dict, plan_created_at) -> tuple[str, str, str] | None:
    """`(action, reason, detail)` if the plan may predate its merged code.

    The server-side twin of `mergesAfterPlan` (frontend/utils/planFreshness),
    plus what that one can afford to ignore: here "could not tell" has to
    stop the post, because nobody is looking at the plan when it goes up.
    """
    if serialized.get("dev_status_unavailable"):
        return FAILED, FAIL_FRESHNESS_UNKNOWN, "Jira's development panel did not answer"
    planned = _parse_ts(plan_created_at)
    if planned is None:
        return FAILED, FAIL_FRESHNESS_UNKNOWN, "the plan has no creation time"
    prs = (serialized.get("development_info") or {}).get("pull_requests") or []
    for pr in prs:
        if not isinstance(pr, dict) or classify_pr_state(pr) != PR_STATE_MERGED:
            continue
        merged = _parse_ts(pr.get("merged_at"))
        if merged is None:
            return (
                FAILED, FAIL_FRESHNESS_UNKNOWN,
                f"no merge time for {pr.get('url') or 'a merged PR'}",
            )
        if merged > planned:
            return (
                SKIPPED, SKIP_STALE_PLAN,
                f"{pr.get('url') or 'a PR'} merged after the plan was written",
            )
    return None


def _own_plan_row(ticket_key: str, rows: list[dict]) -> dict | None:
    """The newest plan written for this ticket alone, as `pickStoredRun` picks it."""
    for row in rows:
        keys = [str(k).upper() for k in row.get("ticket_keys") or []]
        if keys == [ticket_key]:
            return row
    return None


async def auto_post_own_plan(
    jira: JiraClient, ticket_key: str, *, dry_run: bool = False
) -> AutoPostOutcome:
    """Post `ticket_key`'s own plan if nothing is live yet. Never raises.

    `dry_run` runs every check and stops short of posting. Every real outcome
    is recorded per ticket, so a skip in the watcher is visible on the plan.
    """
    key = ticket_key.upper()
    seen: dict = {"plan_id": None}
    try:
        outcome = await _decide_and_post(jira, key, dry_run=dry_run, seen=seen)
    except Exception as exc:
        logger.exception("auto-post failed for %s", key)
        outcome = AutoPostOutcome(
            key, FAILED, "post_failed", f"{type(exc).__name__}: {exc}", seen["plan_id"]
        )
    if not dry_run:
        try:
            await auto_post_repository.record(
                get_db(), ticket_key=key, outcome=outcome.as_dict()
            )
        except Exception:
            # The outcome is still returned and logged; only the plan view's
            # explanation of it is lost.
            logger.exception("auto-post: could not record the outcome for %s", key)
    return outcome


async def _decide_and_post(
    jira: JiraClient, key: str, *, dry_run: bool, seen: dict
) -> AutoPostOutcome:
    db = get_db()
    rows = await plan_repository.list_runs_with_plans_by_ticket(db, ticket_key=key)
    row = _own_plan_row(key, rows)
    if row is None:
        return AutoPostOutcome(key, SKIPPED, SKIP_NO_OWN_PLAN)
    plan_id = seen["plan_id"] = row["plan_id"]

    live = await plan_repository.list_live_plans_for_ticket(db, ticket_key=key)
    own_live = [p for run, p in live if len(run.ticket_keys or []) == 1]
    if own_live:
        return AutoPostOutcome(
            key, SKIPPED, SKIP_ALREADY_LIVE,
            f"plan {own_live[0].id} is already live", plan_id,
        )
    if not row.get("case_count"):
        return AutoPostOutcome(key, SKIPPED, SKIP_EMPTY_PLAN, None, plan_id)

    # The store can miss a post: one from before posts were recorded, or
    # one whose record failed. The comment itself is the final word.
    comments = await jira.get_comments(key)
    if any(_comment_marker_kind(c) is PlanCommentKind.single for c in comments):
        return AutoPostOutcome(key, SKIPPED, SKIP_COMMENT_EXISTS, None, plan_id)

    found = await plan_repository.get_plan_with_cases(db, plan_id=plan_id)
    if found is None:
        return AutoPostOutcome(key, FAILED, "plan_missing", f"plan {plan_id} not found", plan_id)
    stored, _cases = found

    # Last of the checks because it is the slow one: the development panel
    # fans out to GitHub for merge times, 5-14s on a real ticket.
    serialized = plan_service.serialize_issue(await jira.get_issue(key))
    problem = freshness_problem(serialized, stored.created_at)
    if problem:
        action, reason, detail = problem
        return AutoPostOutcome(key, action, reason, detail, plan_id)

    if dry_run:
        return AutoPostOutcome(key, SKIPPED, "dry_run", f"would post plan {plan_id}", plan_id)
    plan = {
        **json.loads(stored.body),
        "plan_id": stored.id,
        "version": stored.version,
        "created_at": stored.created_at.isoformat() if stored.created_at else None,
    }
    walkthrough = await uat_readiness.fetch_readiness(db, ticket_key=key)
    text = await render_jira_text(plan, walkthrough)

    result = await plan_posting.post_one(
        jira,
        issue_key=key,
        comment_text=text,
        plan_id=plan_id,
        kind=PlanCommentKind.single,
    )
    # Read the same fields the Post button's describePostResult reads. A post
    # that stopped partway is marked live by post_one (the first part landed),
    # so reported as POSTED it would show as "Live in Jira" with nothing to
    # say the plan on the ticket is incomplete.
    parts = result.get("parts") or 1
    posted_parts = result.get("posted_parts", parts)
    if posted_parts < parts:
        why = f" — {result['part_error']}" if result.get("part_error") else ""
        return AutoPostOutcome(
            key, FAILED, "partial",
            f"only {posted_parts} of {parts} comments landed{why}", plan_id,
        )
    if result.get("truncated"):
        return AutoPostOutcome(
            key, FAILED, "truncated", "too long for Jira and cut short", plan_id,
        )
    # The comment landed but the store did not record it, so the plan reads
    # "Not live in Jira" in the app while it sits on the ticket. Not a success.
    if result.get("recorded") is False:
        return AutoPostOutcome(
            key, FAILED, "not_recorded",
            f"posted, but {result.get('record_error')}", plan_id,
        )
    logger.info("auto-post: posted plan %s on %s", plan_id, key)
    return AutoPostOutcome(key, POSTED, None, f"plan {plan_id}", plan_id)
