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

* ``watch_require_merged_pr`` — no merged PR means either no diff to ground
  the plan in, or a diff that is still moving. Combined with
  never-regenerate, a plan written against an open PR would outlive the code
  it describes.
* **Not on hold** — a hold is a human parking the ticket, and ``code-review``
  means the PR is still in review. Deferring is free (the next sweep after
  the hold clears still beats the tester to it), so every hold reason skips.
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
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import settings
from ..db.mongo import get_db
from ..jira_client import JiraAuthError, JiraClient, JiraConnectionError
from ..models import GenerateTestPlanRequest
from ..repositories import plan_repository, ticket_hold_repository
from ..source_grounding import PR_STATE_MERGED, classify_pr_state
from . import plan_service

logger = logging.getLogger(__name__)


# Why a candidate was passed over. Surfaced in the sweep result so a
# --dry-run explains itself instead of just printing a shorter list.
SKIP_NON_TESTABLE = "non_testable_issue_type"
SKIP_ALREADY_PLANNED = "already_has_plan"
SKIP_COOLDOWN = "recent_attempt"
SKIP_NO_MERGED_PR = "no_merged_pr"
# The generator itself refused: no merged or open PR to write cases against.
# Distinct from SKIP_NO_MERGED_PR, which is this module declining earlier and
# on a stricter rule (merged only).
SKIP_NO_SOURCE = "no_source"
SKIP_ON_HOLD = "on_hold"
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


def _has_merged_pr(payload: dict) -> bool:
    """Whether the ticket has at least one PR that actually landed.

    Merge state, not mere existence. A plan generated against an open PR is
    written from code that is still changing, and the never-regenerate rule
    then makes that plan permanent — so the tester inherits a plan describing
    code that moved after it was written, with no signal that it did.
    A DECLINED PR is worse: it describes code that will never ship.

    Asks `classify_pr_state`, not `pr["status"]`: Jira's dev-status mirror
    lags GitHub, so a PR that has actually landed can still be reported as
    OPEN. Reading the status string alone made this skip such a ticket on
    every sweep, silently, for as long as the mirror stayed behind.
    """
    dev_info = payload.get("development_info") or {}
    return any(
        classify_pr_state(pr) == PR_STATE_MERGED
        for pr in dev_info.get("pull_requests") or []
        if isinstance(pr, dict)
    )


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
    db = get_db()
    if await plan_repository.has_successful_test_plan(db, ticket_key=ticket_key):
        return SKIP_ALREADY_PLANNED
    # A hold is a human saying "this isn't ready to be worked on" —
    # `code-review` literally means the PR is still in review, so any plan
    # written now is written against code that will change, and
    # never-regenerate would make it permanent.
    #
    # Skipping every hold reason rather than only the code-churn ones,
    # because deferring costs nothing: the ticket stays in the watch
    # status, so the next sweep after the hold clears still gets the plan
    # written before the tester opens it. The worst case is falling back
    # to the pre-watcher behaviour of generating on Pull-to-Testing.
    if await ticket_hold_repository.get_hold(db, ticket_key=ticket_key):
        return SKIP_ON_HOLD
    last_attempt = await plan_repository.find_last_test_plan_attempt_at(
        db, ticket_key=ticket_key
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
    from ..repositories import jira_ticket_repository

    # Claim the at-most-once flag the UI's own auto-dispatch checks. Without
    # this the watcher's analysis is invisible to that check, so the two
    # auto-fire paths disagree about whether a ticket has been analysed.
    try:
        db = get_db()
        await jira_ticket_repository.mark_auto_bug_analysis_dispatched(
            db, ticket_key=payload["ticket_key"]
        )
    except Exception:
        logger.exception(
            "watcher: could not claim auto-bug-analysis for %s; analysing anyway",
            payload["ticket_key"],
        )

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

        if settings.watch_require_merged_pr and not _has_merged_pr(payload):
            result.outcomes.append(
                TicketOutcome(ticket_key, "skipped", SKIP_NO_MERGED_PR)
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

        # The generator found no merged or open PR and declined to write a
        # plan. Reporting that as "generated, 0 cases" would read as a
        # degraded plan; it is a deliberate refusal, and the sweep summary
        # should say so.
        if plan.get("no_source"):
            result.outcomes.append(
                TicketOutcome(ticket_key, "skipped", SKIP_NO_SOURCE)
            )
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


# Token-health state lives on disk, not in memory. In production the watcher
# is a LaunchAgent running `testplan watch --once` — a fresh process every
# five minutes — so an in-process timer would either never fire or fire on
# every sweep. The file is what makes "every six hours" mean anything.
TOKEN_STATE_PATH = Path(
    os.environ.get("WATCH_TOKEN_STATE",
                   Path.home() / ".jira-testplan-bot" / "token-health.json")
)

# Kept for the long-running loop; the file is the source of truth across runs.
_token_health: dict[str, bool] = {}


def _read_token_state() -> dict:
    try:
        return json.loads(TOKEN_STATE_PATH.read_text())
    except Exception:
        # Missing or unreadable means "never checked", which makes the next
        # check happen. Erring toward checking is the safe direction.
        return {}


def _write_token_state(state: dict) -> None:
    try:
        TOKEN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception:
        logger.warning("watcher: could not persist token state to %s; the next "
                       "sweep will re-check", TOKEN_STATE_PATH, exc_info=True)


def notify(title: str, message: str) -> None:
    """Put a message where a person will actually see it.

    The watcher's log goes to ~/Library/Logs and nobody reads it — that is how
    an expired Figma token cost every plan its design context unnoticed. This
    is a local tool on someone's Mac, so the OS notification centre is the
    right surface: no secret, no service, and it cannot be useful to alert
    somewhere remote about a laptop that is asleep, because the watcher is not
    running then either.

    Best-effort and silent on failure: a notification that cannot be delivered
    must never break a sweep.
    """
    if os.environ.get("WATCH_NOTIFY", "").lower() in ("0", "false", "off"):
        return
    if sys.platform != "darwin":
        return

    # Neither of these can confirm delivery — both exit 0 whether or not the
    # notification is shown, because macOS drops them silently when the
    # sending app has no notification permission. terminal-notifier is tried
    # first because it ships its own bundle, so it appears in Notification
    # Centre settings as a grantable app; osascript is attributed to whatever
    # happens to be running it, which under launchd is nothing at all.
    notifier = shutil.which("terminal-notifier")
    cmd = (
        [notifier, "-title", title, "-message", message]
        if notifier
        else ["osascript", "-e",
              f'display notification {json.dumps(message)} with title {json.dumps(title)}']
    )
    try:
        subprocess.run(cmd, capture_output=True, timeout=10, check=False)
    except Exception:
        logger.debug("watcher: notification failed", exc_info=True)


async def check_tokens_if_due(*, force: bool = False) -> list:
    """Run the token check when it is due, reading the clock from disk.

    Called from both entry points — the `--once` sweep launchd actually runs,
    and the long-running loop — because the check belongs to the watcher, not
    to one of its two shapes.
    """
    every_hours = settings.watch_token_check_hours or 0
    if not every_hours and not force:
        return []

    state = _read_token_state()
    last = state.get("last_checked_epoch")
    if not force and isinstance(last, (int, float)):
        if (time.time() - last) < every_hours * 3600:
            return []
    return await check_tokens_once()


async def check_tokens_once() -> list:
    """Check every configured token and log what is wrong, loudly.

    The health check has always existed and nothing ever ran it. That is worth
    more than it sounds: an expired Figma token cost every plan its design
    context for an unknown stretch, and nothing anywhere said so, because each
    downstream failure degraded to the same empty value a ticket with no
    designs produces. A check nobody runs is not a check.

    Never raises — a token check failing must not cost the sweep that follows.
    """
    from ..token_service import TokenHealthService

    try:
        statuses = await TokenHealthService().validate_all_tokens()
    except Exception:
        logger.exception("watcher: token health check raised; skipping this round")
        return []

    state = _read_token_state()
    previous = state.get("health") or {}

    for st in statuses:
        was_ok = _token_health.get(st.service_name, previous.get(st.service_name))
        if not st.is_valid:
            # ERROR every round it stays broken. Mentioning it once is how it
            # gets scrolled past for a month.
            logger.error(
                "watcher: %s token is NOT usable (%s) — %s%s",
                st.service_name,
                getattr(st.error_type, "value", st.error_type),
                st.error_message,
                f" See {st.help_url}" if getattr(st, "help_url", None) else "",
            )
        elif was_ok is False:
            logger.info("watcher: %s token is working again", st.service_name)
        _token_health[st.service_name] = st.is_valid

    broken = [st for st in statuses if not st.is_valid]
    if broken:
        names = ", ".join(st.service_name for st in broken)
        notify(
            "Test Plan Bot: token problem",
            f"{names} not usable. Plans are being generated without it. "
            f"{broken[0].error_message or ''}".strip(),
        )
    elif statuses:
        logger.info("watcher: all %d tokens healthy", len(statuses))
        if previous and not all(previous.get(st.service_name, True) for st in statuses):
            notify("Test Plan Bot", "All tokens are working again.")

    _write_token_state({
        "last_checked_epoch": time.time(),
        "health": {st.service_name: st.is_valid for st in statuses},
    })
    return statuses


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
        await check_tokens_if_due()
        try:
            result = await sweep_once(
                projects=projects, status_name=status_name, dry_run=dry_run
            )
            if on_result:
                on_result(result)
        except Exception:
            logger.exception("watcher: sweep raised; retrying next cycle")
        await asyncio.sleep(interval)
