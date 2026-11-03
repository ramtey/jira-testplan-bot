"""Which happy-path cases a UAT screen recording should cover, and where its
chapters fall.

Why this exists
---------------
The PM watches the video; they do not read the plan. So the deliverable for
them is one recording per ticket that shows the feature working, not a per-case
evidence trail. Two questions have to be answered the same way by two different
callers — the planner's "steps to cover in the video" checklist in the
Pass-to-UAT form, and the UAT runner deciding what to record — and until now
only the first existed, derived in the browser (``videoChecklistSteps`` in
``frontend/src/App.jsx``) from ``plan.happy_path`` titles capped at six.
(That form checklist was removed on 2026-10-27, because the narrated video now
shows what it covers. The plan comment and the runner still read this module.)

A browser-side derivation is the same mistake the progress key made: a second
consumer appears, re-derives the rule, and the two drift with nothing to catch
it. The runner is a separate agent talking to this API, so it could never have
read the React value at all. This module is the one producer; the endpoint and
the plan response both call it.

Why a step budget and not a case count
--------------------------------------
Measured over 386 stored plans: a happy_path section holds a median of 5 cases
and 21 steps, a mean of 5.9 and 28.5 — but the tail runs to 34 cases and 173
steps. At roughly 2.5 seconds of watchable video per step that tail is a
7-minute recording, past Loom's free-tier five-minute ceiling and well past
what anyone watches.

Capping at six cases (what the browser did) bounds the median fine but not the
length, because individual cases run from 1 to 16 steps; it left a 2.8-minute
worst case and covered 72% of plans in full. A 40-step budget bounds the worst
case at about 1.7 minutes and covers 81.9% of plans in full. The budget is the
thing that actually maps to video length, so the budget is what we cap.

Whole cases only. Half a case on film is not a shorter video, it is a video
that stops mid-flow, and the viewer cannot tell which of the two it is.

Why chapters
------------
The happy path is *not* one continuous take. Of the 297 plans whose happy-path
cases declare preconditions, only 47 share a single precondition across every
case; the median plan carries three distinct ones. Recording the section
straight through therefore produces unexplained state resets — the app jumps
back to a different starting point and nothing on screen says why.

So the walkthrough is chaptered, and a chapter starts wherever the precondition
changes. The runner has to re-establish that state to run the case anyway; this
just makes the seam visible to whoever is watching, and gives the sidecar ledger
its timestamps.

Why chapter ids come from ``progress_key``
------------------------------------------
A chapter's ``case_id`` is what the runner marks a result against, so it has to
be the plan's canonical id — the one ``progress_key.case_index`` defines — and
not a position in the raw ``happy_path`` list. Those two differ as soon as a
happy-path case is flagged ``covered_by_unit_test``: the flag lifts it into the
``covered_by_unit_test:<n>`` namespace and every later happy-path case moves up
one. Until 2026-11-03 this module numbered by raw position, so every chapter
after a covered case was off by one. Plan 655 (SK-2948) served ``happy_path:5``
from a plan whose happy path ends at ``happy_path:4``, and its ``happy_path:4``
chapter named a different case than ``happy_path:4`` does: one id that did not
exist and one that pointed at the wrong case.

Why the stored copy is never served as-is
-----------------------------------------
The plan body carries a ``video_walkthrough`` written at generation time. It
is a snapshot of a function of the cases, and a snapshot goes stale the moment
the cases or the id rule change. That is exactly how plan 655's bad ids
outlived the numbering fix. ``walkthrough_from_plan_body`` therefore always
rebuilds from the body's current cases, then checks every chapter against
``case_index`` (id *and* title) and drops any that fails, rather than
returning it. A stale chapter is worse than a missing one: the runner would
file a result against a case it never filmed.
"""

from __future__ import annotations

import logging

from .services import progress_key

logger = logging.getLogger(__name__)

# ~2.5 seconds of watchable video per step (slowMo plus a dwell long enough to
# read the screen). 40 steps is therefore about 100 seconds. See the module
# docstring for why this is a step budget rather than a case count.
DEFAULT_STEP_BUDGET = 40

# Surfaces a screen recording cannot show. A backend_http case is a curl call
# and a cli case is a terminal; filming either produces a video of a terminal
# that proves nothing the response body did not already prove.
_UNFILMABLE_SURFACES = frozenset({"backend_http", "cli", "manual_only"})


def _clean(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _step_count(case: dict) -> int:
    steps = case.get("steps")
    return len(steps) if isinstance(steps, list) else 0


def _is_filmable(case: dict) -> tuple[bool, str | None]:
    """Return ``(filmable, reason_if_not)`` for one happy-path case.

    ``covered_by_unit_test`` cases are excluded because nobody drives them by
    hand — the planner flagged them precisely so they would drop out of the
    manual checklist, and a video of a case the runner never performs would be
    a video of something else.
    """
    if case.get("covered_by_unit_test") is True:
        return False, "covered_by_unit_test"
    surface = _clean(case.get("surface")).lower()
    if surface in _UNFILMABLE_SURFACES:
        return False, f"surface:{surface}"
    if _step_count(case) == 0:
        return False, "no_steps"
    return True, None


def build_walkthrough(
    happy_path,
    *,
    budget: int = DEFAULT_STEP_BUDGET,
) -> dict:
    """Plan the happy-path screen recording for one ticket.

    ``happy_path`` is the plan section as stored — a list of case dicts. Cases
    are taken in plan order, which is already priority order: across 386 stored
    plans ``happy_path[0]`` is ``critical`` in 95.3% and ``high`` in the rest,
    and a later case outranks it in 1.6%. Nothing here re-sorts them.

    Returns::

        {
          "budget": 40,
          "total_steps": 28,        # every filmable happy-path step
          "selected_steps": 28,     # steps the recording actually covers
          "estimated_seconds": 70,
          "truncated": False,       # budget cut the section short
          "omitted": [...],         # cases left out, each with a reason
          "chapters": [
            {"case_id": "happy_path:0", "title": "...", "steps": 5,
             "preconditions": "...", "starts_chapter": True},
            ...
          ],
        }

    ``chapters`` is empty when nothing in the section can be filmed. That is a
    real answer, not a failure — a ticket whose happy path is entirely backend
    or entirely unit-covered has no video to make, and saying so beats handing
    back a recording plan for cases the runner will not perform.
    """
    cases = happy_path if isinstance(happy_path, list) else []
    budget = budget if isinstance(budget, int) and budget > 0 else DEFAULT_STEP_BUDGET

    # Canonical ids, from the one place that defines them. `happy_path` comes
    # first in both of progress_key's orderings (checklist sections and
    # COVERABLE_KEYS), so its ids are the same whether the walk sees this
    # section alone or the whole plan. That is what lets a caller holding only
    # the section get ids that hold in the full plan.
    # Keyed by object identity: the walk yields the very dicts in `cases`.
    canonical = {
        id(item): case_id
        for case_id, _meta, item in progress_key.cases_with_items(
            {"happy_path": cases}
        )
    }

    chapters: list[dict] = []
    omitted: list[dict] = []
    selected_steps = 0
    total_steps = 0
    last_precondition: str | None = None
    # Set once the budget is spent. Everything after it is dropped, including
    # a later case small enough to fit: filming a, skipping b, then filming c
    # shows the happy path out of order, and nothing on screen says a flow is
    # missing. A short video that stops is honest; a stitched one is not.
    budget_spent = False

    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            continue
        case_id = canonical.get(id(case), f"happy_path:{index}")
        title = _clean(case.get("title")) or f"(untitled case {index})"

        filmable, reason = _is_filmable(case)
        if not filmable:
            omitted.append({"case_id": case_id, "title": title, "reason": reason})
            continue

        steps = _step_count(case)
        total_steps += steps

        # Whole cases only, but never return an empty plan just because the
        # first filmable case is on its own larger than the budget. One
        # over-long case is still the recording; the caller sees `truncated`.
        if budget_spent or (chapters and selected_steps + steps > budget):
            budget_spent = True
            omitted.append(
                {"case_id": case_id, "title": title, "reason": "over_budget"}
            )
            continue

        precondition = _clean(case.get("preconditions"))
        # A chapter starts at the first case, and wherever the setup changes —
        # that is exactly where the recording will show a state reset.
        starts_chapter = not chapters or precondition != last_precondition
        last_precondition = precondition

        chapters.append(
            {
                "case_id": case_id,
                "title": title,
                "steps": steps,
                "preconditions": precondition or None,
                "starts_chapter": starts_chapter,
            }
        )
        selected_steps += steps

    return {
        "budget": budget,
        "total_steps": total_steps,
        "selected_steps": selected_steps,
        "estimated_seconds": round(selected_steps * 2.5),
        "truncated": any(o["reason"] == "over_budget" for o in omitted),
        "omitted": omitted,
        "chapters": chapters,
    }


def validate_chapters(walkthrough: dict, body) -> dict:
    """Drop every chapter whose case is not in ``body`` as that exact case.

    A chapter passes when its ``case_id`` is in ``progress_key.case_index`` for
    this body *and* names the case stored under that id. The title check is the
    one that matters most. A shifted id usually still exists; it just addresses
    a different case (plan 655's ``happy_path:4``). An id-only check passes it.

    Dropped chapters move to ``omitted`` with reason ``stale_case_id``, so the
    walkthrough says it lost something rather than looking shorter for no
    reason. ``selected_steps`` and ``estimated_seconds`` are recounted from what
    survives. Returns a new dict; the input is not modified.
    """
    if not isinstance(walkthrough, dict):
        return build_walkthrough([])
    index = progress_key.case_index(body)
    kept: list[dict] = []
    dropped: list[dict] = []
    for chapter in walkthrough.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        case_id = chapter.get("case_id")
        meta = index.get(case_id) if isinstance(case_id, str) else None
        stored_title = _clean(meta.get("title")) if meta else ""
        # An untitled case has nothing to compare; its id alone has to do.
        title_ok = meta is not None and (
            not stored_title or stored_title == _clean(chapter.get("title"))
        )
        if title_ok and meta.get("section") == "happy_path":
            kept.append(chapter)
        else:
            dropped.append(
                {
                    "case_id": case_id,
                    "title": _clean(chapter.get("title")),
                    "reason": "stale_case_id",
                }
            )
    if not dropped:
        return walkthrough
    logger.warning(
        "video_walkthrough: dropped %d chapter(s) not matching the plan: %s",
        len(dropped),
        [d["case_id"] for d in dropped],
    )
    # A dropped chapter may have opened a chapter; the next survivor then has
    # to open one, or the reset it films goes unexplained.
    kept = [dict(c) for c in kept]
    last: object = object()
    for chapter in kept:
        precondition = chapter.get("preconditions")
        chapter["starts_chapter"] = precondition != last
        last = precondition
    selected = sum(c.get("steps") or 0 for c in kept)
    return {
        **walkthrough,
        "selected_steps": selected,
        "estimated_seconds": round(selected * 2.5),
        "omitted": list(walkthrough.get("omitted") or []) + dropped,
        "chapters": kept,
    }


def walkthrough_from_plan_body(body, *, budget: int = DEFAULT_STEP_BUDGET) -> dict:
    """The walkthrough for a stored plan body (parsed JSON, or a dict).

    Always rebuilt from the body's current ``happy_path`` and validated against
    its case ids; the ``video_walkthrough`` stored in the body is ignored. See
    "Why the stored copy is never served as-is" in the module docstring. Every
    reader goes through here: the endpoint, the UAT run view, and plan
    generation itself when it writes the field.
    """
    if not isinstance(body, dict):
        return build_walkthrough([], budget=budget)
    return validate_chapters(build_walkthrough(body.get("happy_path"), budget=budget), body)
