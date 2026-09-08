"""Pre-generate test plans for tickets that land in the QA queue.

The problem this solves
-----------------------
Nothing in this system starts without a human clicking. The Pull-to-Testing
flow does auto-generate a plan, but only *after* the click — so the tester's
first act on a ticket is to sit through a multi-minute Opus run at the exact
moment they wanted to start testing. Multiply that by every ticket in a
sprint and it's the single biggest chunk of dead time in the loop.

So: sweep the QA queue on an interval, and generate the plan (plus Bug Lens
for bugs) while nobody is waiting. By the time the tester opens the ticket,
the plan, the four critics' verdicts and the analysis are already there.

Jira webhooks would be tighter than polling, but they need a publicly
reachable URL and this runs on a laptop. JQL polling costs one search per
project per interval and needs no inbound network.

Spending money unattended
-------------------------
Every generated plan is a real Opus call, so the sweep is deliberately
conservative and each guard is separately configurable:

* ``watch_require_linked_pr`` — no PR means no diff to ground the plan in,
  which is the thin plan QA would rather write by hand.
* ``has_successful_test_plan`` — never regenerate. A ticket gets one
  unattended plan; regeneration stays a human decision.
* ``watch_retry_cooldown_hours`` — a ticket that fails every cycle (a
  timeout, a description the model chokes on) would otherwise burn a call
  every interval forever.
* ``watch_max_per_cycle`` — bounds the damage when a sprint's worth of
  tickets moves to Ready to Test at once.

The checks run cheapest-first: the DB dedupe is free, the Jira fetch costs
a few hundred milliseconds, and only what survives both reaches the LLM.

One watcher process is assumed. Two would race — the dedupe is a read, not
a claim — which is fine for a single laptop and is the thing to fix (a
claim column, following ``jira_tickets.auto_bug_analysis_dispatched_at``)
before this is deployed anywhere shared.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..config import settings
from ..db.session import get_sessionmaker
from ..jira_client import JiraAuthError, JiraClient, JiraConnectionError
from ..models import GenerateTestPlanRequest
from ..repositories import plan_repository
from . import plan_service

logger = logging.getLogger(__name__)


# Why a candidate was passed over. Surfaced in the sweep result so a
# --dry-run explains itself instead of just printing a shorter list.
SKIP_NON_TESTABLE = "non_testable_issue_type"
SKIP_ALREADY_PLANNED = "already_has_plan"
SKIP_COOLDOWN = "recent_attempt"
SKIP_NO_LINKED_PR = "no_linked_pr"
SKIP_CAP_REACHED = "cycle_cap_reached"


@dataclass
class TicketOutcome:
    """What the sweep did, or didn't do, with one ticket."""

    ticket_key: str
    action: str  # "generated" | "skipped" | "failed"
    reason: str | None = None
    detail: str | None = None


@dataclass
class SweepResult:
    outcomes: list[TicketOutcome] = field(default_factory=list)
    scanned: int = 0
    # Set when the queue itself couldn't be read, which is different from
    # "the queue was empty" and shouldn't be reported as a quiet success.
    queue_error: str | None = None

    @property
    def generated(self) -> list[TicketOutcome]:
        return [o for o in self.outcomes if o.action == "generated"]

    @property
    def failed(self) -> list[TicketOutcome]:
        return [o for o in self.outcomes if o.action == "failed"]

    @property
    def skipped(self) -> list[TicketOutcome]:
        return [o for o in self.outcomes if o.action == "skipped"]


def watched_projects() -> list[str]:
    """Projects to sweep — explicit setting, else the workflow prefixes.

    Reusing ``workflow_project_prefixes`` as the fallback means a team that
    already configured the QA workflow buttons doesn't have to name their
    projects a second time.
    """
    return [p.upper() for p in (settings.watch_projects or settings.workflow_project_prefixes)]


def _has_linked_pr(payload: dict) -> bool:
    dev_info = payload.get("development_info") or {}
    return bool(dev_info.get("pull_requests"))


async def _find_queue(
    project_key: str, status_name: str, *, jira: JiraClient
) -> list[str]:
    """Ticket keys sitting in `status_name` for `project_key`, board order."""
    rows = await jira.search_project_issues(project_key, status_name)
    return [row.key for row in rows]


async def _screen(ticket_key: str) -> str | None:
    """DB-only checks. Returns a skip reason, or None to keep going.

    Runs before the Jira fetch because it's free and rejects the common
    case — a ticket that already has a plan.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if await plan_repository.has_successful_test_plan(session, ticket_key=ticket_key):
            return SKIP_ALREADY_PLANNED
        last_attempt = await plan_repository.find_last_test_plan_attempt_at(
            session, ticket_key=ticket_key
        )
    if last_attempt is not None and settings.watch_retry_cooldown_hours > 0:
        # created_at may come back naive depending on the driver; treat a
        # naive timestamp as UTC rather than crashing the whole sweep.
        if last_attempt.tzinfo is None:
            last_attempt = last_attempt.replace(tzinfo=timezone.utc)
        cooldown = timedelta(hours=settings.watch_retry_cooldown_hours)
        if datetime.now(timezone.utc) - last_attempt < cooldown:
            return SKIP_COOLDOWN
    return None


async def _dispatch_bug_lens(payload: dict, serialized: dict) -> None:
    """Kick off Bug Lens for a Bug ticket whose plan just landed.

    Calls the route handler directly, which is backwards layering and
    deliberate: Bug Lens's pipeline (GitHub context, code evidence, blame
    anchors, persistence) still lives inline in its route the way test-plan
    generation used to. Extracting a ``bug_lens_service`` the way
    ``plan_service`` was extracted is the right fix; until then, calling the
    handler is what keeps the watcher's analysis identical to the UI's
    rather than a second, thinner reimplementation.

    Best-effort — a failed analysis must not cost the plan we just made.
    """
    from ..bug_lens_routes import analyze_bug
    from ..models import BugAnalysisRequest

    try:
        await analyze_bug(
            BugAnalysisRequest(
                ticket_key=payload["ticket_key"],
                summary=payload["summary"],
                description=payload.get("description"),
                issue_type=payload["issue_type"],
                development_info=payload.get("development_info"),
                comments=payload.get("comments"),
                parent_info=payload.get("parent_info"),
                linked_info=payload.get("linked_info"),
                status=serialized.get("status"),
                status_category=serialized.get("status_category"),
            )
        )
        logger.info("watcher: bug lens analysed %s", payload["ticket_key"])
    except Exception:
        logger.exception(
            "watcher: bug lens failed for %s; plan is unaffected",
            payload["ticket_key"],
        )


async def sweep_once(
    *,
    projects: list[str] | None = None,
    status_name: str | None = None,
    dry_run: bool = False,
    max_per_cycle: int | None = None,
) -> SweepResult:
    """One pass over the QA queue.

    With ``dry_run`` every guard still runs — the sweep just stops short of
    the LLM call, so the reported skips and the would-be generations are the
    same ones a real run would produce.
    """
    projects = projects or watched_projects()
    status_name = status_name or settings.watch_status
    cap = settings.watch_max_per_cycle if max_per_cycle is None else max_per_cycle
    result = SweepResult()

    jira = JiraClient()
    queue: list[str] = []
    for project_key in projects:
        try:
            queue.extend(await _find_queue(project_key, status_name, jira=jira))
        except (JiraAuthError, JiraConnectionError) as e:
            # Can't read the board — say so rather than reporting an empty
            # queue as "nothing to do".
            result.queue_error = f"{type(e).__name__}: {e}"
            logger.error("watcher: could not read %s queue: %s", project_key, e)
        except Exception as e:
            result.queue_error = f"{type(e).__name__}: {e}"
            logger.exception("watcher: unexpected error reading %s queue", project_key)

    result.scanned = len(queue)
    generated = 0

    for ticket_key in queue:
        if generated >= cap:
            result.outcomes.append(
                TicketOutcome(ticket_key, "skipped", SKIP_CAP_REACHED)
            )
            continue

        skip = await _screen(ticket_key)
        if skip:
            result.outcomes.append(TicketOutcome(ticket_key, "skipped", skip))
            continue

        try:
            issue = await jira.get_issue(ticket_key)
        except Exception as e:
            result.outcomes.append(
                TicketOutcome(ticket_key, "failed", "fetch_failed", str(e))
            )
            logger.exception("watcher: fetch failed for %s", ticket_key)
            continue

        serialized = plan_service.serialize_issue(issue)
        payload = plan_service.prompt_payload(serialized)

        if settings.watch_require_linked_pr and not _has_linked_pr(payload):
            result.outcomes.append(
                TicketOutcome(ticket_key, "skipped", SKIP_NO_LINKED_PR)
            )
            continue

        if dry_run:
            result.outcomes.append(
                TicketOutcome(ticket_key, "generated", "dry_run", payload["summary"])
            )
            generated += 1
            continue

        try:
            plan = await plan_service.generate_single(
                GenerateTestPlanRequest(**payload)
            )
        except plan_service.NonTestableIssueError:
            result.outcomes.append(
                TicketOutcome(ticket_key, "skipped", SKIP_NON_TESTABLE)
            )
            continue
        except Exception as e:
            result.outcomes.append(
                TicketOutcome(ticket_key, "failed", "generate_failed", str(e))
            )
            logger.exception("watcher: generation failed for %s", ticket_key)
            continue

        case_count = sum(
            len(plan.get(section) or [])
            for section in ("happy_path", "edge_cases", "integration_tests")
        )
        result.outcomes.append(
            TicketOutcome(
                ticket_key,
                "generated",
                detail=f"{case_count} cases, plan {plan.get('plan_id', '?')}",
            )
        )
        generated += 1
        logger.info("watcher: generated plan for %s (%d cases)", ticket_key, case_count)

        # Bugs get their analysis queued up too, so the tester finds both
        # waiting rather than kicking off Bug Lens by hand after reading.
        if (payload.get("issue_type") or "").lower() == "bug":
            await _dispatch_bug_lens(payload, serialized)

    return result


async def watch(
    *,
    projects: list[str] | None = None,
    status_name: str | None = None,
    interval_seconds: int | None = None,
    dry_run: bool = False,
    on_result=None,
) -> None:
    """Sweep forever. ``on_result`` is called with each ``SweepResult``.

    A sweep that raises never kills the loop — the queue being unreadable
    for one cycle (laptop asleep, Jira blip, expired token) should cost the
    next cycle nothing.
    """
    interval = interval_seconds or settings.watch_interval_seconds
    while True:
        try:
            result = await sweep_once(
                projects=projects, status_name=status_name, dry_run=dry_run
            )
            if on_result:
                on_result(result)
        except Exception:
            logger.exception("watcher: sweep raised; retrying next cycle")
        await asyncio.sleep(interval)
