"""Carrying QA marks across a regeneration, case by case.

Why this exists
---------------
Progress is keyed by a fingerprint of the plan's section sizes, so regenerating
a plan whose shape changed moves every mark to a fresh key. That is deliberate —
``progress_key`` exists because stale checks landing on a *different* case is
the worst thing this app can do — but the consequence had no remedy. SK-2325
regenerated from ``SK-2325:4-6-2-10`` (plan 532) to ``SK-2325:5-8-0-9-4`` (plan
536) and stranded sixteen marks. The tester re-mapped them by hand: some cases
had moved section (the zero/false-retention checks went from ``edge_cases`` to
``happy_path``), some had been reworded, and two had no equivalent at all.

``/test-plan-progress/{key}/other-shapes`` could already *say* the marks existed.
It deliberately refused to move them, on the grounds that "which checks still
apply is a judgement only a tester can make". That is right about the judgement
and wrong about the conclusion: the tester was left to make it against two
comment bodies in two browser tabs. This module makes the same judgement
available as a proposal — here is what you ticked, here is the case it looks
like now, here is where the wording changed, here is what has no equivalent —
and carries over only what the tester confirms.

How a match is proposed
-----------------------
Every mark is resolved against the plan it was *recorded against*, not the plan
on screen. Finding that plan is the whole trick: a progress row stores only its
key, so the key of each stored plan for the same ticket(s) is recomputed (one
producer, ``build_progress_key``) and the row joined to whichever plan produces
it. A row whose plan is gone can still be reported by count, but its individual
cases cannot be named, and this says so rather than guessing.

Titles are then matched: identical after normalisation is ``same``; a close but
not identical title is ``reworded`` and is flagged for reading, never carried
silently; anything below the threshold is ``gone``. A case that changed section
still matches — it is the same test — which is exactly the zero/false-retention
move that cost the most hand-mapping on SK-2325.

Nothing here writes. ``apply`` does, and only for the ids it is handed.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.app.db import crud
from src.app.db.models.run import Run as RunModel
from src.app.repositories import plan_repository, test_plan_progress_repository
from src.app.services import progress_key as progress_key_service

# Below this, two titles are different tests rather than one reworded test. Set
# by hand against the SK-2325 pairs: "Zero values are retained, not coerced to
# blank" vs "Zero and false are retained on save" scores 0.62 and is the same
# check; the closest genuinely-unrelated pair in that plan scores 0.44.
REWORD_THRESHOLD = 0.55

_WORD = re.compile(r"[^a-z0-9]+")


def _normalize(title: str) -> str:
    return _WORD.sub(" ", (title or "").lower()).strip()


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _checked_ids(row: Any) -> list[str]:
    try:
        parsed = json.loads(row.checked_ids or "[]")
    except (ValueError, TypeError):
        return []
    return [i for i in parsed if isinstance(i, str)] if isinstance(parsed, list) else []


def _propose(source_case: dict, target_index: dict[str, dict], taken: set[str]) -> dict:
    """The best unclaimed case in the new plan for one old case."""
    source_title = _normalize(source_case.get("title", ""))
    best_id: str | None = None
    best_score = 0.0
    for candidate_id, candidate in target_index.items():
        if candidate_id in taken:
            continue
        score = _similarity(source_title, _normalize(candidate.get("title", "")))
        # A tie goes to the case that stayed in the same section; a move between
        # sections is real and common, but it is the less likely reading.
        if score > best_score or (
            score == best_score
            and best_id is not None
            and candidate.get("section") == source_case.get("section")
            and target_index[best_id].get("section") != source_case.get("section")
        ):
            best_score = score
            best_id = candidate_id
    if best_id is None or best_score < REWORD_THRESHOLD:
        return {"status": "gone", "to": None, "similarity": round(best_score, 3)}
    return {
        "status": "same" if best_score >= 0.999 else "reworded",
        "to": best_id,
        "similarity": round(best_score, 3),
    }


async def build(db: AsyncIOMotorDatabase, *, plan_id: int) -> dict:
    """Propose a carry-over for `plan_id` from the newest progress under another
    shape of the same ticket(s). Read-only."""
    result = await plan_repository.get_plan_with_cases(db, plan_id=plan_id)
    if result is None:
        return {"error": "plan_not_found"}
    plan, _cases = result
    run = await crud.get_by_id(db, RunModel, plan.run_id)
    if run is None:
        return {"error": "run_not_found"}

    ticket_keys = list(run.ticket_keys or [])
    current_key = progress_key_service.build_progress_key(ticket_keys, plan.body)
    target_index = progress_key_service.case_index(plan.body)

    rows = await test_plan_progress_repository.find_for_same_tickets(
        db, progress_key=current_key
    )
    sources = [r for r in rows if r.progress_key != current_key.upper() and _checked_ids(r)]
    if not sources:
        return {
            "plan_id": plan_id,
            "progress_key": current_key,
            "source": None,
            "marks": [],
        }

    # `find_for_same_tickets` sorts newest first, so the first row with marks is
    # the last shape anyone actually tested.
    source_row = sources[0]
    source_ids = _checked_ids(source_row)

    # Which stored plan produced that key? Recomputing each candidate's key from
    # the one producer is the only honest join — the row itself records nothing
    # but the string.
    #
    # The current plan is a candidate too, and deliberately so: a row written
    # before the covered count joined the key sits under *this* plan's legacy
    # fingerprint, and excluding the plan from its own history is how SK-2325's
    # sixteen marks would have gone from orphaned-but-named to unreadable.
    source_index: dict[str, dict] = {}
    source_plan_id: int | None = None
    wanted = source_row.progress_key.upper()
    for candidate, candidate_tickets in await plan_repository.find_plans_for_tickets(
        db, ticket_keys=ticket_keys
    ):
        keys = [
            progress_key_service.build_progress_key(candidate_tickets, candidate.body),
            *progress_key_service.legacy_progress_keys(candidate_tickets, candidate.body),
        ]
        if any(key.upper() == wanted for key in keys):
            source_index = progress_key_service.case_index(candidate.body)
            source_plan_id = candidate.id
            break

    marks: list[dict] = []
    taken: set[str] = set()
    for old_id in source_ids:
        source_case = source_index.get(old_id)
        if source_case is None:
            # The plan that gave this id is gone, or the id predates the current
            # id space. Report it; do not invent a target for it.
            marks.append(
                {
                    "from": old_id,
                    "from_title": None,
                    "from_section": old_id.rsplit(":", 1)[0] if ":" in old_id else None,
                    "status": "unresolved",
                    "to": None,
                    "to_title": None,
                    "to_section": None,
                    "similarity": 0.0,
                }
            )
            continue
        proposal = _propose(source_case, target_index, taken)
        target = target_index.get(proposal["to"]) if proposal["to"] else None
        if proposal["to"]:
            taken.add(proposal["to"])
        marks.append(
            {
                "from": old_id,
                "from_title": source_case.get("title"),
                "from_section": source_case.get("section"),
                "status": proposal["status"],
                "to": proposal["to"],
                "to_title": target.get("title") if target else None,
                "to_section": target.get("section") if target else None,
                "to_optional": bool(target.get("optional")) if target else False,
                "similarity": proposal["similarity"],
            }
        )

    return {
        "plan_id": plan_id,
        "progress_key": current_key,
        "source": {
            "progress_key": source_row.progress_key,
            "fingerprint": source_row.progress_key.rsplit(":", 1)[-1],
            "plan_id": source_plan_id,
            "checked_count": len(source_ids),
            "updated_at": source_row.updated_at.isoformat()
            if source_row.updated_at
            else None,
            "resolvable": bool(source_index),
        },
        "marks": marks,
    }


async def apply(
    db: AsyncIOMotorDatabase, *, plan_id: int, ids: list[str]
) -> dict:
    """Union `ids` into the current plan's progress.

    Every id is checked against the current plan's own case index first. An id
    that names no case is refused rather than stored: a row the UI reads and
    then matches against nothing is the invisible-check failure this whole area
    keeps being fixed for, and a carry-over is the one operation most likely to
    produce one.
    """
    result = await plan_repository.get_plan_with_cases(db, plan_id=plan_id)
    if result is None:
        return {"error": "plan_not_found"}
    plan, _cases = result
    run = await crud.get_by_id(db, RunModel, plan.run_id)
    if run is None:
        return {"error": "run_not_found"}

    key = progress_key_service.build_progress_key(list(run.ticket_keys or []), plan.body)
    valid = progress_key_service.case_index(plan.body)
    unknown = [i for i in ids if i not in valid]
    if unknown:
        return {"error": "unknown_ids", "unknown": unknown}

    existing_row = await test_plan_progress_repository.get_progress(db, progress_key=key)
    existing = _checked_ids(existing_row) if existing_row is not None else []
    merged = sorted(set(existing) | set(ids))
    added = sorted(set(ids) - set(existing))
    row = await test_plan_progress_repository.upsert_progress(
        db, progress_key=key, checked_ids=merged
    )
    return {
        "plan_id": plan_id,
        "progress_key": key,
        "checked_ids": _checked_ids(row),
        "added": added,
    }
