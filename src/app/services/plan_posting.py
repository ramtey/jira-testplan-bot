"""Posting a generated plan to Jira, for one ticket or for a batch.

Two shapes:

* **single** — the plan was written for one ticket. It goes in that ticket's
  own comment slot, exactly as it always has.
* **batch** — the plan covers several tickets. It goes to one *host* ticket
  (chosen by `batch_plan_host` when the plan was generated) and every other
  ticket gets a short pointer to it.

The batch plan and a ticket's own plan occupy different comment slots, so
posting one never overwrites the other. That separation is the point of the
module: before it, a batch post reused whatever plan comment it found and
replaced the plan written for that ticket alone.
"""

from __future__ import annotations

import json
import logging

from src.app.db.mongo import get_db
from src.app.jira_client import (
    JiraClient,
    PlanCommentKind,
    _comment_marker_kind,
    plan_version_note,
)
from src.app.repositories import plan_repository
from src.app.services.batch_plan_host import BatchHostCandidate, choose_batch_host

logger = logging.getLogger(__name__)


# Sentinel for "the plan's coverage could not be read", as distinct from a
# plan that recorded none for this ticket.
COVERAGE_UNREADABLE = object()


def _coverage_line(coverage) -> str | None:
    """What the batch plan checked about this ticket's own ACs.

    A pointer that only says "the plan is over there" reads as coverage. On
    SK-2627 it was posted on a ticket whose ACs the batch plan never
    extracted, so nothing had checked that a single one of them was tested —
    and two of them shipped broken.
    """
    if coverage is None:
        return None
    if coverage is COVERAGE_UNREADABLE:
        return (
            "The batch plan's coverage of this ticket's acceptance criteria "
            "could not be loaded, so it is not summarised here."
        )
    total = int(coverage.get("total") or 0)
    if total == 0:
        return (
            "**No acceptance criteria could be read from this ticket, so nothing "
            "checked that the batch plan covers it.** Review the plan against "
            "this ticket's description by hand before signing it off."
        )
    uncovered = coverage.get("uncovered") or []
    under = coverage.get("under_covered") or []
    covered = total - len(uncovered)
    line = f"The batch plan covers {covered} of this ticket's {total} acceptance criteria."
    gaps = [f"{u.get('id')} (not covered)" for u in uncovered] + [
        f"{u.get('id')} ("
        + (
            f"no case asserts where it lands: {u['missing_destination']}"
            if u.get("missing_destination")
            else "partly covered: " + ", ".join(u.get("missing_actions") or [])
        )
        + ")"
        for u in under
    ]
    if gaps:
        line += " **Gaps:** " + "; ".join(gaps) + "."
    return line


def _pointer_text(host_key: str, covered_keys: list[str], coverage=None) -> str:
    """The body of a non-host ticket's signpost comment.

    Short on purpose. It is not a plan and must not read like one — the tester
    needs to know where the plan is and why it is not here, in the two lines
    Jira shows without expanding anything. ``coverage`` is this ticket's entry
    in the batch plan's ``ac_coverage``, so the pointer says what was checked
    rather than implying everything was.
    """
    others = ", ".join(covered_keys)
    text = (
        f"**The plan for this ticket is on {host_key}.**\n\n"
        f"It covers {others} together, and is posted once so there is one copy "
        f"to work through and check off rather than an identical copy on every "
        f"ticket in the batch.\n\n"
    )
    coverage_line = _coverage_line(coverage)
    if coverage_line:
        text += coverage_line + "\n\n"
    return text + (
        "If this ticket also has its own test plan comment, that one is "
        "separate and unaffected."
    )


async def _load_ticket_coverage(plan_id: int | None) -> dict | None:
    """``ac_coverage['tickets']`` of a stored batch plan, keyed by ticket.

    Returns ``{}`` when there is no stored plan to read, and None when the
    read failed — a lookup that failed is not a plan with no ACs.
    """
    if plan_id is None:
        return {}
    try:
        found = await plan_repository.get_plan_with_cases(get_db(), plan_id=plan_id)
        if found is None:
            return {}
        plan, _cases = found
        tickets = (json.loads(plan.body).get("ac_coverage") or {}).get("tickets") or {}
        return {k.upper(): v for k, v in tickets.items()}
    except Exception:
        logger.exception("Could not read ac_coverage for plan %s", plan_id)
        return None


async def post_one(
    jira: JiraClient,
    *,
    issue_key: str,
    comment_text: str,
    plan_id: int | None,
    kind: PlanCommentKind = PlanCommentKind.single,
    adopt_legacy_batch: bool = False,
    record_as_live: bool = True,
) -> dict:
    """Post one comment and record which plan version is now live on it.

    Returns the response body the HTTP layer hands back. Every partial outcome
    is named in it rather than collapsed into success — a comment that landed
    while the "this version is live" write failed is the silent success this
    path keeps being fixed for, so `recorded` is tri-state and `record_error`
    carries the reason.
    """
    # Read the version before posting, because the version rides on the
    # comment's marker line. Posting a regeneration edits the existing
    # comment, which leaves Jira's `created` at the original date and moves
    # only `updated` — a watcher sees no new activity and can conclude the
    # regeneration failed (SK-2325, comments 332346/332347).
    version_note: str | None = None
    if plan_id is not None and kind is not PlanCommentKind.pointer:
        try:
            existing = await plan_repository.get_plan_with_cases(
                get_db(), plan_id=plan_id
            )
            if existing:
                version_note = plan_version_note(existing[0].version)
        except Exception:
            logging.exception(
                "Could not read plan %s to version its Jira comment", plan_id
            )

    result = await jira.post_comment(
        issue_key,
        comment_text,
        version_note=version_note,
        kind=kind,
        adopt_legacy_batch=adopt_legacy_batch,
    )
    comment_id = result.get("id")
    posted_at_iso: str | None = None

    # Tri-state. None: there was nothing to record — no plan_id, or this is a
    # pointer, which is a signpost and not a version of anything. True: this
    # version is now marked as the one live in Jira. False: the comment landed
    # but the mark did not, so the plan is on the ticket while the version
    # badge still reads "Not live in Jira", and re-posting repeats it
    # identically because the write fails the same way every time.
    recorded: bool | None = None
    record_error: str | None = None
    should_record = (
        plan_id is not None
        and record_as_live
        and kind is not PlanCommentKind.pointer
    )
    if should_record:
        if not comment_id:
            recorded = False
            record_error = "Jira did not return a comment id"
        else:
            db = None
            try:
                db = get_db()
                await plan_repository.mark_plan_posted_to_jira(
                    db,
                    plan_id=plan_id,
                    ticket_key=issue_key.upper(),
                    jira_comment_id=str(comment_id),
                    batch=kind is PlanCommentKind.batch,
                )
                recorded = True
            except Exception:
                # Posting succeeded; failing to record the mark shouldn't fail
                # the request. The tester keeps their posted plan — they are
                # told the app could not record it.
                recorded = False
                record_error = "the app could not reach its database"
                logging.exception(
                    "Failed to mark plan %s posted on %s", plan_id, issue_key
                )
            if recorded:
                # Read-back only, for the timestamp. The mark is what makes
                # this version live; losing its timestamp here does not unmake
                # it, so this failure must never flip `recorded`.
                try:
                    plan_with_cases = await plan_repository.get_plan_with_cases(
                        db, plan_id=plan_id
                    )
                    if plan_with_cases and plan_with_cases[0].posted_at:
                        posted_at_iso = plan_with_cases[0].posted_at.isoformat()
                except Exception:
                    logging.exception(
                        "Marked plan %s posted on %s but could not read it back",
                        plan_id,
                        issue_key,
                    )

    return {
        "success": True,
        "comment_id": comment_id,
        "issue_key": issue_key,
        "updated": result.get("updated", False),
        "truncated": result.get("truncated", False),
        # A plan too big for one Jira comment is posted across several. The
        # response is an explicit allowlist, so every field the client needs to
        # describe a partial or split post has to be named here — left out,
        # they default to "one comment, all of it landed", which is the silent
        # success this path keeps being fixed for.
        "parts": result.get("parts", 1),
        "posted_parts": result.get("posted_parts", 1),
        "part_comment_ids": result.get("part_comment_ids", []),
        "stale_parts_left": result.get("stale_parts_left", 0),
        "part_error": result.get("part_error"),
        "version_note": result.get("version_note"),
        "kind": result.get("kind", kind.value),
        "adopted_legacy": result.get("adopted_legacy", False),
        # An oversized plan is one index comment plus the full text attached.
        # `attachment_error` set means that attach failed and the plan went up
        # split instead, which the client has to explain.
        "attachment_filename": result.get("attachment_filename"),
        "attachment_error": result.get("attachment_error"),
        "plan_id": plan_id,
        "posted_at": posted_at_iso,
        "recorded": recorded,
        "record_error": record_error,
    }


async def resolve_batch_host(
    *, plan_id: int | None, selected_keys: list[str]
) -> tuple[str, str | None]:
    """`(host_key, recorded_host)` for a batch post over `selected_keys`.

    Prefers the host recorded on the run when the plan was generated — that is
    what stops the plan migrating between tickets as branch counts change. It
    is only overridden when the recorded host is not among the tickets being
    posted to, which happens when the tester unticks it.

    The fallback has no branch counts to work from (nothing has fetched the
    development panels at post time), so it falls through to the tie-break
    rule: the lowest key. Stable and explicable, which is what matters for a
    choice that gets recorded and reused.
    """
    recorded_host: str | None = None
    if plan_id is not None:
        try:
            run = await plan_repository.get_run_for_plan(get_db(), plan_id=plan_id)
            if run is not None:
                recorded_host = (run.batch_host_key or None)
        except Exception:
            logging.exception("Could not read the recorded batch host for plan %s", plan_id)

    if recorded_host and recorded_host.upper() in {k.upper() for k in selected_keys}:
        return recorded_host.upper(), recorded_host.upper()

    fallback = choose_batch_host([BatchHostCandidate(key=k) for k in selected_keys])
    return fallback, (recorded_host.upper() if recorded_host else None)


async def _clear_batch_comments(jira: JiraClient, issue_key: str) -> int:
    """Delete any batch plan comments on `issue_key`. Returns how many went.

    Used wherever a ticket stops being the host. The plan comment and the
    pointer live in different slots, so posting a pointer does not displace a
    plan that is already there — without this the ticket ends up carrying both,
    a full plan and a note saying the plan is somewhere else.

    Raises if a delete fails, so the caller can leave the pointer unwritten
    rather than add a signpost beside a plan that is still there and still
    looks current.
    """
    comments = await jira.get_comments(issue_key)
    stale_ids = [
        str(c.get("id"))
        for c in comments
        if _comment_marker_kind(c) is PlanCommentKind.batch and c.get("id")
    ]
    for stale_id in stale_ids:
        await jira.delete_comment(issue_key, stale_id)
    return len(stale_ids)


async def post_batch(
    jira: JiraClient,
    *,
    plan_id: int | None,
    comment_text: str,
    ticket_keys: list[str],
) -> dict:
    """Post a multi-ticket plan: the plan to its host, a pointer to the rest.

    Ordering is deliberate. The host is posted first and the pointers only go
    out if it landed — a pointer written before the plan exists sends the
    tester to a ticket that has nothing on it, and on a ticket carrying a
    pre-split batch comment the pointer would have replaced the only copy of
    the plan with a signpost to somewhere it never arrived.
    """
    keys = [k.strip().upper() for k in ticket_keys if k and k.strip()]
    if not keys:
        raise ValueError("post_batch needs at least one ticket key")

    host_key, recorded_host = await resolve_batch_host(
        plan_id=plan_id, selected_keys=keys
    )

    host_result = await post_one(
        jira,
        issue_key=host_key,
        comment_text=comment_text,
        plan_id=plan_id,
        kind=PlanCommentKind.batch,
        # The pre-split batch comment on this ticket is plain-marked and would
        # otherwise sit in the single-ticket slot waiting to be overwritten by
        # the next single-ticket post.
        adopt_legacy_batch=True,
    )

    pointers: list[dict] = []
    pointer_errors: list[dict] = []
    demoted: list[str] = []
    if host_result.get("posted_parts", 0) > 0:
        coverage_by_key = await _load_ticket_coverage(plan_id)
        for key in keys:
            if key == host_key:
                continue
            try:
                # This ticket may be the previous host — the recorded host can
                # change while every ticket stays selected, which
                # `_retire_previous_host` does not cover because it only looks
                # at tickets dropped from the batch. Its plan comes down before
                # its pointer goes up, or it would carry both.
                if await _clear_batch_comments(jira, key):
                    demoted.append(key)
                pointers.append(
                    await post_one(
                        jira,
                        issue_key=key,
                        comment_text=_pointer_text(
                            host_key,
                            keys,
                            COVERAGE_UNREADABLE
                            if coverage_by_key is None
                            else coverage_by_key.get(key),
                        ),
                        plan_id=plan_id,
                        kind=PlanCommentKind.pointer,
                        # Converts this ticket's copy of the old all-tickets
                        # batch plan into the pointer, instead of leaving it
                        # behind as a second, now-stale plan.
                        adopt_legacy_batch=True,
                    )
                )
            except Exception as exc:
                # The plan is on the host, which is what matters. A ticket
                # without its pointer is reported, not raised: failing the
                # whole call here would describe a successful post as a failed
                # one.
                logger.warning("Could not leave a batch pointer on %s: %s", key, exc)
                pointer_errors.append({"issue_key": key, "error": str(exc)})

    moved_from = await _retire_previous_host(
        jira,
        recorded_host=recorded_host,
        host_key=host_key,
        selected_keys=keys,
        pointer_body=_pointer_text(host_key, keys),
    )

    return {
        "success": True,
        "scope": "batch",
        "host_key": host_key,
        "host": host_result,
        "pointers": pointers,
        "pointer_errors": pointer_errors,
        "ticket_keys": keys,
        # Set when the plan moved off a ticket that was dropped from the batch
        # entirely, so the client can say where it went rather than leaving the
        # tester to find a pointer where a plan used to be.
        "moved_from": moved_from,
        # Tickets still in the batch that were carrying the plan and are now
        # carrying a pointer instead. Reported for the same reason: someone
        # reading the old host needs to be told the plan moved.
        "demoted": demoted,
    }


async def _retire_previous_host(
    jira: JiraClient,
    *,
    recorded_host: str | None,
    host_key: str,
    selected_keys: list[str],
    pointer_body: str,
) -> str | None:
    """Turn a dropped host's batch comment into a pointer at the new host.

    Only fires when the recorded host was left out of this post, so the plan
    has moved. Without it that ticket keeps a full batch plan that no longer
    gets updated — a second, silently stale copy, which is the drift the
    single-host layout exists to remove.

    Deliberately narrow: it acts only on a batch comment the bot already
    wrote, and never creates one on a ticket that had none. A ticket the tester
    unticked should not gain a comment it did not have.
    """
    if not recorded_host or recorded_host == host_key:
        return None
    if recorded_host in {k.upper() for k in selected_keys}:
        return None
    try:
        if not await _clear_batch_comments(jira, recorded_host):
            return None
    except Exception as exc:
        # Leave the pointer unwritten rather than add a signpost next to a full
        # plan that is still there and still looks current.
        logger.warning(
            "Could not retire the batch comment on %s: %s", recorded_host, exc
        )
        return None

    try:
        await post_one(
            jira,
            issue_key=recorded_host,
            comment_text=pointer_body,
            plan_id=None,
            kind=PlanCommentKind.pointer,
        )
    except Exception as exc:
        logger.warning("Could not retire the batch comment on %s: %s", recorded_host, exc)
        return None
    return recorded_host
