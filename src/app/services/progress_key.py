"""The canonical progress key for a stored plan, and the canonical id of a case.

Why this exists
---------------
``test_plan_progress`` is keyed by a composite of the ticket key(s) and a
fingerprint of the plan's section sizes. Until now that key had two independent
producers that disagreed:

* The frontend built it from ``displayPlan`` — the plan with cases the planner
  flagged ``covered_by_unit_test`` removed, because those are pulled out of the
  manual checklist (``TestPlanDisplay.jsx``).
* ``uat-runner/scripts/mark-passed.sh`` built it from ``--counts``, four numbers
  typed by whoever ran it.

Commit 5332f54 ("Flag test cases already covered by unit tests", 2026-06-25)
introduced the filtering and repointed the frontend's fingerprint at it without
touching the script, so from that day the two producers disagreed for any plan
containing a covered case. The runner would mark cases under a key the UI never
reads: SK-2642 was written as ``SK-2642:4-8-2-7`` while the UI polled
``SK-2642:4-8-1-7`` and rendered 0/20.

Deriving the key here — from the stored plan, on the server — leaves one
producer. That only became true on 2026-09-22: the first pass added this module
and repointed the script at it, but left ``buildStorageKey`` in
``TestPlanDisplay.jsx`` computing the key from ``displayPlan``, so the browser
remained a second producer and this docstring conceded as much ("must stay in
step with"). SK-2327 is what the survivor cost. The browser was rendering a plan
the database had never seen — a leftover cached one — derived ``SK-2327:3-4-0-7``
from it, and showed 0% while eleven cases sat passed under ``SK-2327:4-5-0-6``.

The frontend now asks ``GET /plans/{plan_id}/progress-key`` and uses the answer
verbatim; it still counts its own sections, but only to compare against the
``fingerprint`` returned here and warn when the two disagree, rather than to
build a key. A plan with no stored run has no key at all and is said to be
untracked, because inventing one is how progress gets written where nothing
reads it. ``tests/test_progress_key.py`` pins the shape, the 404, and the
sibling lookup the UI uses to tell "untested" from "tested under a previous
plan shape"; ``frontend/src/utils/planState.test.js`` pins the precedence rule
that keeps a cached plan from shadowing the stored one in the first place.

The fifth number
----------------
Removing a covered case from the checklist also removed it from the *id space*:
it had no ``section:index`` at all, so nothing could record that a tester had
run it anyway. SK-2325's plan 536 is the case that made that untenable — all
four of its ``integration_tests`` cases were flagged covered, the section
rendered as 0, and two of them were then verified live with real evidence that
had nowhere to go (``mark-passed.sh`` answered "'integration_tests:0' is out of
range").

Covered cases are now *optional, not absent*. They keep their own namespace,
``covered_by_unit_test:<n>``, numbered across the card sections in
``COVERABLE_KEYS`` order — the same flat order the UI lists them in — and the
fingerprint grows a fifth component for them. The four manual sections are
counted exactly as before, so a plan with no covered cases keeps the key it
already had, byte for byte, and only plans that contain covered cases (the ones
whose evidence was unrecordable anyway) move to a new key. ``mark_carryover``
is the path across that move.

Section sizes as the key still mean any regeneration that changes a section's
length orphans the prior progress. That is deliberate — stale checks must not
land on a different set of cases — but it is no longer silent, and
``/plans/{id}/mark-carryover`` now offers the tester a case-by-case way across.
"""

from __future__ import annotations

import json
from typing import Any

# Order matters: the fingerprint is these four counts joined by "-".
SECTION_KEYS = (
    "happy_path",
    "edge_cases",
    "integration_tests",
    "regression_checklist",
)

# Sections whose cases the planner can flag as already covered by a unit test.
# Those are lifted out of the manual checklist into `COVERED_KEY`, so they must
# not be counted in their own section's size.
# `regression_checklist` is deliberately absent — it carries no such flag.
COVERABLE_KEYS = ("happy_path", "edge_cases", "integration_tests")

# The fifth namespace: cases lifted out of the manual sections because a unit
# test already asserts them. They are optional — excluded from the "how much of
# the checklist is done" denominator — but they are addressable, so a tester who
# runs one anyway has somewhere to record it.
COVERED_KEY = "covered_by_unit_test"

# Every id space a checked id may live in. `mark-passed.sh` validates against
# this list, and anything outside it is a typo, not a case.
ALL_SECTION_KEYS = SECTION_KEYS + (COVERED_KEY,)


def _as_plan_dict(body: Any) -> dict:
    """Plan bodies are stored as text; only the ``json`` format carries sections."""
    if isinstance(body, dict):
        return body
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _is_covered(item: Any) -> bool:
    return isinstance(item, dict) and bool(item.get("covered_by_unit_test"))


def _items(plan: dict, key: str) -> list:
    items = plan.get(key)
    return items if isinstance(items, list) else []


def _title_of(item: Any) -> str:
    """The human-readable title of a case.

    Regression entries are bare strings rather than case objects (they skip
    every grounding guard for the same reason), so a title lookup has to cope
    with both shapes.
    """
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        title = item.get("title")
        if isinstance(title, str):
            return title
        if title is not None:
            return json.dumps(title, sort_keys=True)
    return ""


def _visible_length(plan: dict, key: str) -> int:
    items = _items(plan, key)
    if key not in COVERABLE_KEYS:
        return len(items)
    return sum(1 for item in items if not _is_covered(item))


def covered_length(body: Any) -> int:
    """How many cases were lifted out of the manual sections as unit-tested."""
    plan = _as_plan_dict(body)
    return sum(
        1 for key in COVERABLE_KEYS for item in _items(plan, key) if _is_covered(item)
    )


def fingerprint(body: Any) -> str:
    """The section-size fingerprint for a plan body.

    ``h-e-i-r`` for a plan with no unit-tested cases, ``h-e-i-r-c`` when it has
    ``c`` of them. The fifth component is omitted rather than written as ``-0``
    so that every plan without covered cases — the great majority — keeps the
    exact key it already had and its recorded progress with it.
    """
    plan = _as_plan_dict(body)
    parts = [str(_visible_length(plan, key)) for key in SECTION_KEYS]
    covered = covered_length(plan)
    if covered:
        parts.append(str(covered))
    return "-".join(parts)


def build_progress_key(ticket_keys: list[str], body: Any) -> str:
    """The full key: ticket keys joined by ``+``, then the fingerprint.

    Ticket-key order is the run's own order (plan-generation order), which is what
    the frontend uses for multi-ticket plans.
    """
    tickets = "+".join(k.upper() for k in ticket_keys if k)
    return f"{tickets}:{fingerprint(body)}"


def legacy_fingerprints(body: Any) -> list[str]:
    """Fingerprints this same plan produced under earlier versions of the rule.

    Exactly one entry so far: before 2026-09-23 the covered count was not part
    of the key, so a plan with covered cases was ``h-e-i-r`` where it is now
    ``h-e-i-r-c``. A plan with none has no legacy form — its key never moved.

    This exists so a ``test_plan_progress`` row written under the old rule can
    still be traced back to the plan it was recorded against. Without it,
    SK-2325's sixteen marks under ``SK-2325:5-8-0-9`` would have become
    unreadable the moment plan 536 started deriving ``SK-2325:5-8-0-9-4`` — the
    orphan notice would say marks exist and `mark_carryover` would be unable to
    name a single one of them, which is a worse place than before the change.

    The id space did not move with the key: covered cases were simply absent
    from it, and every other case keeps the index it had. So a row matched this
    way maps onto the current plan one id at a time.
    """
    plan = _as_plan_dict(body)
    if not covered_length(plan):
        return []
    return ["-".join(str(_visible_length(plan, key)) for key in SECTION_KEYS)]


def legacy_progress_keys(ticket_keys: list[str], body: Any) -> list[str]:
    """Full keys this plan produced under earlier versions of the rule."""
    tickets = "+".join(k.upper() for k in ticket_keys if k)
    return [f"{tickets}:{fp}" for fp in legacy_fingerprints(body)]


def case_index(body: Any) -> dict[str, dict]:
    """Map every canonical case id in a plan to what it addresses.

    This is the one definition of "``edge_cases:3`` means *that* case". The UI
    renders it on each card, the Jira comment prints it beside each title, and
    ``mark_carryover`` matches across plan versions with it — so a tester
    reading a label, an agent reading the comment, and the row in
    ``test_plan_progress`` all name the same thing.

    Returns ``{id: {"section", "index", "title", "optional"}}`` in render order.
    """
    plan = _as_plan_dict(body)
    index: dict[str, dict] = {}
    for section in SECTION_KEYS:
        position = 0
        for item in _items(plan, section):
            if section in COVERABLE_KEYS and _is_covered(item):
                continue
            index[f"{section}:{position}"] = {
                "section": section,
                "index": position,
                "title": _title_of(item),
                "optional": False,
            }
            position += 1
    position = 0
    for section in COVERABLE_KEYS:
        for item in _items(plan, section):
            if not _is_covered(item):
                continue
            index[f"{COVERED_KEY}:{position}"] = {
                "section": COVERED_KEY,
                "index": position,
                "title": _title_of(item),
                "optional": True,
                "origin_section": section,
                "unit_test_ref": item.get("unit_test_ref") or None,
            }
            position += 1
    return index
