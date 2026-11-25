"""Post a ticket's plan to Jira when the ticket enters testing.

Two triggers share this module. Pull to testing calls it right after the
transition; the watcher sweep calls it for tickets dragged into the column on
the board, which nothing here sees happen. Both go through `auto_post_own_plan`
so they skip the same tickets for the same reasons.

With AUTO_POST_ON_QUEUE the QA queue (`watch_status`, Ready to Test) is a
posting status too: the watcher posts a plan it just generated there at once,
and the sweep covers a ticket dragged into the queue that already had a plan.

What it posts is deliberately narrow: the ticket's own single-ticket plan,
onto a ticket that has no plan comment yet — or, for a regeneration, over the
comment this app recorded for the older live version, edited in place as the
Update Jira button does. Everything else is left to a person:

* **A version a person chose** — the live plan was posted after the newer one
  was generated, so someone picked it over the newer one. A regeneration
  generated after the live post is not such a choice: Jira would keep a plan
  that no longer matches the store, which is what testers run (SK-2835,
  SK-3059). Every skip of a newer version says "vN not posted to Jira".
* **A plan comment the app did not record** — in the slot with no record, or
  not the comment recorded for the live plan. Never overwritten.
* **No plan of its own** — still generating, never generated, or covered only
  by a batch plan, whose host is the tester's call.
* **A plan with no cases** — this repo's recurring failure is a transient
  error saved as a plan (an empty tool_use, an off-VPN run). Posting it would
  put that failure on the ticket looking like a finished plan.
* **A plan whose grounding PRs moved** — new commits, closed unmerged, or
  merged code that differs from what the plan read (pr_state_recheck). A PR
  that only merged, with the code the plan read, refreshes the plan instead.
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
import os
import shutil
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.app.config import settings
from src.app.db.mongo import get_db
from src.app.jira_client import JiraClient, PlanCommentKind, _comment_marker_kind
from src.app.repositories import auto_post_repository, plan_repository
from src.app.pr_state_recheck import PHASE_POST, recheck_pr_states
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
# A PR the plan is grounded on moved since it was written (new commits,
# closed, or merged code that differs): src/app/pr_state_recheck.py.
SKIP_GROUNDING_MOVED = "grounding_moved"
# Not an auto-post outcome: a plan generated before its ticket reached
# testing. History only, so "nothing logged" never has to be explained.
SKIP_NOT_IN_TESTING = "not_in_testing"
FAIL_FRESHNESS_UNKNOWN = "freshness_unknown"
# Another process holds the ticket's posting lease. Not recorded: the holder
# records its own outcome, and this one would race it for the slot.
SKIP_POST_IN_PROGRESS = "post_in_progress"
# Longer than the slowest post (dev panel 5-14s, formatter up to 30s, a
# multi-part comment), short enough that a crashed holder costs one sweep.
CLAIM_LEASE_SECONDS = 300
# A POSTED outcome that edited the live older version's comment in place.
POSTED_UPDATED = "updated"

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


def posting_statuses() -> list[str]:
    """The statuses whose tickets get their plan posted, in sweep order.

    `auto_post_status` (empty switches auto-post off altogether), then the QA
    queue when AUTO_POST_ON_QUEUE is set.
    """
    primary = (settings.auto_post_status or "").strip()
    if not primary:
        return []
    statuses = [primary]
    queue = (settings.watch_status or "").strip()
    if settings.auto_post_on_queue and queue and queue.lower() != primary.lower():
        statuses.append(queue)
    return statuses


def posts_on(status: str | None) -> bool:
    """Whether a ticket in `status` gets its plan posted."""
    current = (status or "").strip().lower()
    return bool(current) and any(current == s.lower() for s in posting_statuses())


def _is_queue_status(status: str | None) -> bool:
    queue = (settings.watch_status or "").strip().lower()
    return bool(queue) and (status or "").strip().lower() == queue


async def queue_if_in_testing(background_tasks, ticket_key: str, plan: dict) -> dict | None:
    """Queue the post for a plan just generated on a ticket already in testing.

    Closes the gap where Pull to testing ran before a plan existed: the
    transition's own post found nothing, and the plan generated afterwards
    waited for the sweep. Same `auto_post_own_plan`, so the same refusals.

    Returns {"action": "queued"} when queued, else None. A status read that
    fails queues nothing and is logged; the sweep still picks the ticket up.
    """
    if not posting_statuses() or plan.get("plan_id") is None or plan.get("no_source"):
        return None
    key = ticket_key.upper()
    try:
        current = await JiraClient().get_issue_status(key)
    except Exception:
        logger.warning("auto-post: could not read %s's status after generating", key, exc_info=True)
        return None
    if not posts_on(current):
        await log_not_in_testing(key, plan, current, trigger=TRIGGER_GENERATE)
        return None
    background_tasks.add_task(auto_post_own_plan, JiraClient(), key, trigger=TRIGGER_GENERATE)
    return {"action": "queued"}


async def note_watcher_plan(ticket_key: str, plan: dict) -> AutoPostOutcome | None:
    """Post a plan the watcher just generated, or record why it waits. Never raises.

    The watcher generates for the QA queue. With AUTO_POST_ON_QUEUE a ticket
    still in the queue gets the plan posted here, through the same
    `auto_post_own_plan` refusals; without it the plan waits for testing and
    that is logged. Status is read again here, not taken from before
    generating: a run takes minutes. A ticket pulled into testing meanwhile is
    the sweep's to post (it waits out Pull to testing's own post), and a
    no_source refusal or an unsaved plan posts nothing.
    """
    if not posting_statuses() or plan.get("plan_id") is None or plan.get("no_source"):
        return None
    key = ticket_key.upper()
    try:
        current = await JiraClient().get_issue_status(key)
    except Exception:
        logger.warning("auto-post: could not read %s's status after the watcher generated", key, exc_info=True)
        return None
    if _is_queue_status(current) and posts_on(current):
        return await auto_post_own_plan(JiraClient(), key, trigger=TRIGGER_WATCHER)
    if not posts_on(current):
        await log_not_in_testing(key, plan, current, trigger=TRIGGER_WATCHER)
    return None


async def log_not_in_testing(
    ticket_key: str, plan: dict, current: str | None, *, trigger: str
) -> None:
    """Add a history row for a plan left waiting for testing. Never raises.

    History only, not the ticket's recorded outcome: that drives the Post
    control and the sweep's change detection, and this is not a decision
    either of them made.
    """
    status = " or ".join(posting_statuses()) or settings.auto_post_status
    try:
        await auto_post_repository.add_event(
            get_db(),
            ticket_key=ticket_key,
            event={
                "action": SKIPPED,
                "reason": SKIP_NOT_IN_TESTING,
                "detail": f"ticket is in {current or 'an unknown status'}; posts when it moves to {status}",
                "plan_id": plan.get("plan_id"),
                "trigger": trigger,
            },
        )
    except Exception:
        logger.exception("auto-post: could not log the waiting plan on %s", ticket_key)


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


def freshness_problem(
    serialized: dict, plan_created_at, accounted_for: set[str] | frozenset[str] = frozenset()
) -> tuple[str, str, str] | None:
    """`(action, reason, detail)` if the plan may predate its merged code.

    The server-side twin of `mergesAfterPlan` (frontend/utils/planFreshness),
    plus what that one can afford to ignore: here "could not tell" has to
    stop the post, because nobody is looking at the plan when it goes up.

    `accounted_for` holds the URLs of PRs the PR state re-check already
    confirmed merged with the code the plan read, and refreshed the plan for.
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
        if merged > planned and pr.get("url") not in accounted_for:
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


# Which door an auto-post came through, for `testplan auto-post`.
TRIGGER_PULL = "pull_to_testing"
TRIGGER_GENERATE = "generate"
TRIGGER_SWEEP = "sweep"
TRIGGER_MANUAL = "manual"
TRIGGER_WATCHER = "watcher"


async def log_manual_post(ticket_key: str, result: dict) -> None:
    """Record a Post to Jira click in the history. Never raises.

    The point is the question auto-post leaves open: after a regeneration, do
    people always click Update Jira? If so, that update should be automatic.
    """
    if result.get("plan_id") is None:
        return
    try:
        await auto_post_repository.add_event(
            get_db(),
            ticket_key=ticket_key,
            event={
                "action": POSTED,
                "reason": None,
                "detail": "updated" if result.get("updated") else "new comment",
                "plan_id": result["plan_id"],
                "trigger": TRIGGER_MANUAL,
            },
        )
    except Exception:
        logger.exception("could not log the manual post on %s", ticket_key)


async def auto_post_own_plan(
    jira: JiraClient,
    ticket_key: str,
    *,
    dry_run: bool = False,
    trigger: str = TRIGGER_SWEEP,
) -> AutoPostOutcome:
    """Post `ticket_key`'s own plan, or update the live comment in place with
    a regeneration nobody chose against. Never raises.

    `dry_run` runs every check and stops short of posting. Every real outcome
    is recorded per ticket, so a skip in the watcher is visible on the plan.
    """
    key = ticket_key.upper()
    seen: dict = {"plan_id": None}
    # A dry run posts nothing, so it takes no lease and never blocks a post.
    owner = None if dry_run else f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    try:
        if owner and not await auto_post_repository.claim(
            get_db(), ticket_key=key, owner=owner, lease_seconds=CLAIM_LEASE_SECONDS
        ):
            logger.info("auto-post: %s is being posted by another process; skipped", key)
            return AutoPostOutcome(
                key, SKIPPED, SKIP_POST_IN_PROGRESS, "another auto-post is posting this ticket"
            )
    except Exception as exc:
        # Posting without the lease is the race it exists to stop, and a
        # database that cannot take a claim could not record the post either.
        logger.exception("auto-post: could not claim %s", key)
        return AutoPostOutcome(key, FAILED, "claim_failed", f"{type(exc).__name__}: {exc}")
    try:
        outcome = await _decide_and_post(jira, key, dry_run=dry_run, seen=seen)
    except Exception as exc:
        logger.exception("auto-post failed for %s", key)
        outcome = AutoPostOutcome(
            key, FAILED, "post_failed", f"{type(exc).__name__}: {exc}", seen["plan_id"]
        )
    finally:
        if owner:
            try:
                await auto_post_repository.release(get_db(), ticket_key=key, owner=owner)
            except Exception:
                # The lease runs out on its own; until then the ticket skips.
                logger.exception("auto-post: could not release the claim on %s", key)
    if not dry_run:
        try:
            previous = await auto_post_repository.record(
                get_db(), ticket_key=key, outcome=outcome.as_dict()
            )
        except Exception:
            # The outcome is still returned and logged; only the plan view's
            # explanation of it is lost. No alert either: without the previous
            # outcome there is no telling a new problem from a repeat.
            logger.exception("auto-post: could not record the outcome for %s", key)
        else:
            if _is_new(outcome, previous):
                _alert(outcome)
                try:
                    await auto_post_repository.add_event(
                        get_db(), ticket_key=key,
                        event={**outcome.as_dict(), "trigger": trigger},
                    )
                except Exception:
                    logger.exception("auto-post: could not log the event for %s", key)
    return outcome


# Outcomes worth interrupting someone for. Everything else is either a post,
# a normal "nothing to do yet", or a choice a person already made.
_ALERT_SKIPS = {SKIP_STALE_PLAN, SKIP_EMPTY_PLAN, SKIP_GROUNDING_MOVED}


def _alert_text(outcome: AutoPostOutcome) -> str:
    if outcome.reason == SKIP_STALE_PLAN:
        return f"{outcome.detail}. Regenerate, or post it as it is."
    if outcome.reason == SKIP_GROUNDING_MOVED:
        # A manual post is refused for the same reason, so "post it as it is"
        # is not on offer here.
        return f"{outcome.detail}."
    if outcome.reason == SKIP_EMPTY_PLAN:
        return "The plan has no cases, so it was not posted."
    if outcome.reason in ("partial", "truncated"):
        return f"Incomplete in Jira: {outcome.detail}. Re-post from the app."
    return f"Not posted: {outcome.detail or outcome.reason}."


def _is_new(outcome: AutoPostOutcome, previous: dict | None) -> bool:
    """Whether `outcome` differs from the one it replaced.

    The sweep re-checks a ticket every few minutes for a day, so anything done
    per outcome — an alert, a history row — would repeat hundreds of times.
    New means a different action, reason or plan, so a fix followed by the
    same failure counts again.
    """
    return previous is None or (
        previous.get("action"), previous.get("reason"), previous.get("plan_id")
    ) != (outcome.action, outcome.reason, outcome.plan_id)


def _alert(outcome: AutoPostOutcome) -> None:
    """A desktop alert for an outcome someone has to act on. Never raises."""
    if outcome.action != FAILED and outcome.reason not in _ALERT_SKIPS:
        return
    try:
        # Imported here: queue_watcher imports this module.
        from src.app.services.queue_watcher import notify

        notify(f"Test Plan Bot: {outcome.ticket_key} not posted", _alert_text(outcome))
    except Exception:
        logger.exception("auto-post: could not alert about %s", outcome.ticket_key)


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
    # A regeneration replaces the live version in place: the tester follows
    # the Jira comment, and a comment older than the store's plan is a plan
    # nobody is running any more (SK-2835 kept v1's 4 cases under a 28-case
    # v2; SK-3059 the same). A version a person posted on purpose — posted
    # after the newer one already existed — is still left alone.
    replacing = own_live[0] if own_live else None
    if replacing is not None:
        if replacing.id == plan_id or not _is_newer(row, replacing):
            return AutoPostOutcome(
                key, SKIPPED, SKIP_ALREADY_LIVE, f"plan {replacing.id} is already live", plan_id,
            )
        if not _is_unposted_regeneration(row, replacing):
            return AutoPostOutcome(
                key, SKIPPED, SKIP_ALREADY_LIVE,
                f"{_version(row.get('version'))} not posted to Jira: plan {replacing.id} "
                f"({_version(replacing.version)}) was posted after it was generated, so it was chosen",
                plan_id,
            )
    if not row.get("case_count"):
        return AutoPostOutcome(key, SKIPPED, SKIP_EMPTY_PLAN, _not_posted(row, replacing), plan_id)

    # The store can miss a post: one from before posts were recorded, or
    # one whose record failed. The comment itself is the final word. When a
    # regeneration replaces a live plan, the comment in the slot must be the
    # one recorded for that plan — anything else was not posted by this app
    # for this plan, and is not overwritten.
    comments = await jira.get_comments(key)
    slot = [str(c.get("id")) for c in comments if _comment_marker_kind(c) is PlanCommentKind.single]
    if slot and (replacing is None or slot[0] != str(replacing.jira_comment_id or "")):
        detail = None if replacing is None else (
            f"{_not_posted(row, replacing)}: the plan comment on the ticket ({slot[0]}) is not the one "
            f"recorded for plan {replacing.id} ({replacing.jira_comment_id or 'none'})"
        )
        return AutoPostOutcome(key, SKIPPED, SKIP_COMMENT_EXISTS, detail, plan_id)

    found = await plan_repository.get_plan_with_cases(db, plan_id=plan_id)
    if found is None:
        return AutoPostOutcome(key, FAILED, "plan_missing", f"plan {plan_id} not found", plan_id)
    stored, _cases = found

    # Re-read every PR the plan is grounded on (src/app/pr_state_recheck.py).
    # New commits, an abandoned PR, or merged code that differs from what the
    # plan read: not posted. A PR that only merged: the plan is refreshed in
    # place, and that merge no longer makes the plan stale below.
    body = json.loads(stored.body)
    report = await recheck_pr_states(body, phase=PHASE_POST, created_at=stored.created_at)
    if report["refuse"]:
        return AutoPostOutcome(
            key, SKIPPED, SKIP_GROUNDING_MOVED,
            _prefixed(_not_posted(row, replacing), "; ".join(report["refuse"])), plan_id,
        )
    refreshed = {c["url"] for c in report["changes"]}

    # Last of the checks because it is the slow one: the development panel
    # fans out to GitHub for merge times, 5-14s on a real ticket.
    serialized = plan_service.serialize_issue(await jira.get_issue(key))
    problem = freshness_problem(serialized, stored.created_at, accounted_for=refreshed)
    if problem:
        action, reason, detail = problem
        return AutoPostOutcome(key, action, reason, _prefixed(_not_posted(row, replacing), detail), plan_id)

    if dry_run:
        verb = f"would update plan {replacing.id}'s comment in place with" if replacing else "would post"
        return AutoPostOutcome(key, SKIPPED, "dry_run", f"{verb} plan {plan_id}", plan_id)
    if report["modified"]:
        await plan_repository.update_plan_body(db, plan_id=plan_id, body=json.dumps(body))
    plan = {
        **body,
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
    if replacing is not None:
        logger.info("auto-post: plan %s replaced plan %s in place on %s", plan_id, replacing.id, key)
        return AutoPostOutcome(
            key, POSTED, POSTED_UPDATED,
            f"plan {plan_id} ({_version(stored.version)}) updated in place "
            f"(replaces {_version(replacing.version)}, plan {replacing.id})",
            plan_id,
        )
    logger.info("auto-post: posted plan %s on %s", plan_id, key)
    return AutoPostOutcome(key, POSTED, None, f"plan {plan_id}", plan_id)


def _version(v) -> str:
    return f"v{v}" if v is not None else "this version"


def _is_newer(row: dict, live) -> bool:
    return (row.get("version") or 0, row["plan_id"]) > ((live.version or 0), live.id)


def _is_unposted_regeneration(row: dict, live) -> bool:
    """Whether the newest own plan `row` is a regeneration nobody chose against.

    Newer than the live plan, and generated after that plan went up. A live
    plan posted after `row` existed was picked over it by a person (or a
    re-post of the old version), and stays.
    """
    if not _is_newer(row, live):
        return False
    created = _parse_ts(row.get("created_at"))
    posted = _parse_ts(getattr(live, "posted_at", None))
    if created is None or posted is None:
        # Cannot tell which came first: the newer version is the plan.
        return True
    return created > posted


def _not_posted(row: dict, replacing) -> str | None:
    """"vN not posted to Jira", when a live older version stays on the ticket."""
    if replacing is None:
        return None
    return (f"{_version(row.get('version'))} not posted to Jira "
            f"(Jira still shows {_version(replacing.version)}, plan {replacing.id})")


def _prefixed(head: str | None, detail: str | None) -> str | None:
    if not head:
        return detail
    return f"{head}: {detail}" if detail else head
