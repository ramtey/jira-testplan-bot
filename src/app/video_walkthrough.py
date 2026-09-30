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
"""

from __future__ import annotations

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
        case_id = f"happy_path:{index}"
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


def walkthrough_from_plan_body(body, *, budget: int = DEFAULT_STEP_BUDGET) -> dict:
    """Build the walkthrough from a stored plan body (parsed JSON, or a dict).

    Plans generated before this module existed carry no precomputed
    walkthrough, so the endpoint recomputes from ``happy_path`` rather than
    reporting nothing for the whole back catalogue.
    """
    if not isinstance(body, dict):
        return build_walkthrough([], budget=budget)
    existing = body.get("video_walkthrough")
    if isinstance(existing, dict) and isinstance(existing.get("chapters"), list):
        return existing
    return build_walkthrough(body.get("happy_path"), budget=budget)
