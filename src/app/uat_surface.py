"""Where a plan's cases run: on a screen, or only against an API and database.

Why this exists
---------------
The Pass-to-UAT gate asks for a walkthrough (a Loom, a screenshot, notes) on
every ``uat_complexity: "high"`` ticket. A walkthrough is a recording of the
UI, so on a ticket with no UI it asks for something that cannot exist.
SK-3014 (plan 734) changed lock columns, a Postgres RPC and API logic. Every
case was ``backend_http`` or ``cli``, the video walkthrough builder found no
chapters to film, and the gate still returned 409 until the tester sent the
override, which made the override routine and the gate meaningless.

Why the per-case ``surface`` and not the video chapters
-------------------------------------------------------
Every case in a generated plan carries a required ``surface`` (the tool schema
in ``llm_client`` lists it under ``required``): ``backend_http``, ``web_ui``,
``mobile``, ``cli`` or ``manual_only``. It is the planner's own statement of
what a runner needs to execute the case, and other checks already key on it
(``deployed_env_checks``, ``build_targets``, ``surface_inputs``).

The video builder (``video_walkthrough``) was the other candidate. It reads
the same field, but only on ``happy_path``: a plan whose happy path is all API
calls and whose edge cases include a ``web_ui`` check would read as "nothing
to film" and lose its gate. Walking every manual section avoids that.

What each value means
---------------------
``ui``       every case is ``web_ui`` or ``mobile``.
``mixed``    some cases are on a screen and some are not.
``backend``  every case is ``backend_http`` or ``cli``. Nothing to film.
``manual``   no case is on a screen, but at least one is ``manual_only``. A
             manual check can be visual (an App Store page, a rendered Slack
             message, an email), so the gate stays, but written evidence
             counts: notes, or a non-image Jira attachment such as a results
             summary or an evidence zip.
``unknown``  no manual cases, or a case with no ``surface`` (plans written
             before the field was required). Treated as before: gated.

Cases flagged ``covered_by_unit_test`` are skipped, since nobody runs them by
hand. ``regression_checklist`` items are bare strings with no surface and are
skipped too: they are re-checks of existing behaviour, not the change the
walkthrough is meant to show.
"""

from __future__ import annotations

from typing import Any

from .services import progress_key

UI_SURFACES = frozenset({"web_ui", "mobile"})
BACKEND_SURFACES = frozenset({"backend_http", "cli"})
MANUAL_SURFACES = frozenset({"manual_only"})

UI = "ui"
MIXED = "mixed"
BACKEND = "backend"
MANUAL = "manual"
UNKNOWN = "unknown"


def classify(body: Any) -> str:
    """Return the plan's UAT surface: ``ui``, ``mixed``, ``backend``,
    ``manual`` or ``unknown``. ``body`` is the stored JSON text or a dict."""
    has_ui = has_backend = has_manual = has_unlabelled = False
    seen = 0
    for _case_id, meta, item in progress_key.cases_with_items(body):
        if meta.get("optional") or not isinstance(item, dict):
            continue
        seen += 1
        surface = item.get("surface")
        surface = surface.strip().lower() if isinstance(surface, str) else ""
        if surface in UI_SURFACES:
            has_ui = True
        elif surface in BACKEND_SURFACES:
            has_backend = True
        elif surface in MANUAL_SURFACES:
            has_manual = True
        else:
            has_unlabelled = True
    if seen == 0:
        return UNKNOWN
    if has_ui:
        return MIXED if (has_backend or has_manual or has_unlabelled) else UI
    if has_unlabelled:
        return UNKNOWN
    if has_manual:
        return MANUAL
    return BACKEND if has_backend else UNKNOWN
