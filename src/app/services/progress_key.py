"""The canonical progress key for a stored plan.

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

Deriving the key here — from the stored plan, on the server — leaves exactly one
producer. The rule below must stay in step with ``buildStorageKey`` and
``displayPlan`` in ``TestPlanDisplay.jsx``; ``tests/test_progress_key.py`` pins
the shape.
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
# Those are lifted out of the manual checklist, so they must not be counted.
# `regression_checklist` is deliberately absent — it carries no such flag.
COVERABLE_KEYS = frozenset({"happy_path", "edge_cases", "integration_tests"})


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


def _visible_length(plan: dict, key: str) -> int:
    items = plan.get(key)
    if not isinstance(items, list):
        return 0
    if key not in COVERABLE_KEYS:
        return len(items)
    return sum(
        1
        for item in items
        if not (isinstance(item, dict) and item.get("covered_by_unit_test"))
    )


def fingerprint(body: Any) -> str:
    """The ``h-e-i-r`` section-size fingerprint for a plan body."""
    plan = _as_plan_dict(body)
    return "-".join(str(_visible_length(plan, key)) for key in SECTION_KEYS)


def build_progress_key(ticket_keys: list[str], body: Any) -> str:
    """The full key: ticket keys joined by ``+``, then the fingerprint.

    Ticket-key order is the run's own order (plan-generation order), which is what
    the frontend uses for multi-ticket plans.
    """
    tickets = "+".join(k.upper() for k in ticket_keys if k)
    return f"{tickets}:{fingerprint(body)}"
