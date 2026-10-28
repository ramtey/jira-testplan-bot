from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.app.db import crud
from src.app.db.models.case_result import CaseResult
from src.app.db.models.test_plan_progress import TestPlanProgress
from src.app.repositories import case_result_repository


@dataclass
class ProgressView:
    """The checked set for one progress key, as every reader should see it.

    Shaped like the ``TestPlanProgress`` row it replaces for readers
    (``progress_key``, ``checked_ids`` as a JSON string, ``updated_at``) so
    callers did not have to change. It is not a document: nothing saves it.
    """

    progress_key: str
    checked_ids: str
    updated_at: datetime | None


def counts_towards_checked(result: CaseResult) -> bool:
    """Whether `result` changes what a tester sees as checked.

    A person's result and a legacy mark always do. An agent's result does only
    once a person has approved it — before that it is a proposal shown beside
    the checklist, and it must not tick a box, nor untick one a person ticked.
    """
    kind = (result.actor or {}).get("kind")
    if kind == "agent":
        return bool(result.review_approved)
    return True


def fold(baseline: Iterable[str], results: Iterable[CaseResult]) -> list[str]:
    """Apply `results`, oldest first, to the legacy `baseline` marks.

    A pass checks a case; anything else (cleared, failed, not run) unchecks it.
    """
    checked = {c for c in baseline if isinstance(c, str)}
    for result in results:
        if not counts_towards_checked(result):
            continue
        if result.status == "passed":
            checked.add(result.case_id)
        else:
            checked.discard(result.case_id)
    return sorted(checked)


def _baseline(row: TestPlanProgress | None) -> list[str]:
    if row is None:
        return []
    try:
        parsed = json.loads(row.checked_ids or "[]")
    except (ValueError, TypeError):
        return []
    return [c for c in parsed if isinstance(c, str)] if isinstance(parsed, list) else []


def _view(key: str, row: TestPlanProgress | None, results: list[CaseResult]) -> ProgressView | None:
    if row is None and not results:
        return None
    stamps = [r.created_at for r in results if r.created_at]
    if row is not None and row.updated_at:
        stamps.append(row.updated_at)
    return ProgressView(
        progress_key=key,
        checked_ids=json.dumps(fold(_baseline(row), results)),
        updated_at=max(stamps) if stamps else None,
    )


async def get_progress(
    db: AsyncIOMotorDatabase,
    *,
    progress_key: str,
) -> ProgressView | None:
    """The checked set for `progress_key`, or None if nothing was ever recorded.

    The ``test_plan_progress`` row holds marks written before results existed;
    it is a frozen baseline now, and no code path writes it. Results recorded
    since are applied on top in order.
    """
    key = progress_key.upper()
    row = await crud.find_one(db, TestPlanProgress, {"progress_key": key})
    results = await case_result_repository.list_for_key(db, progress_key=key)
    return _view(key, row, results)


async def record_marks(
    db: AsyncIOMotorDatabase,
    *,
    progress_key: str,
    plan_id: int | None,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
    actor: dict[str, Any],
    source: str,
) -> tuple[ProgressView | None, list[str], list[str]]:
    """Record a pass for each id in `add` and a clear for each in `remove`.

    Only ids whose state actually changes get a row, so re-sending a mark is a
    no-op rather than a second result. Returns the new view plus the ids
    really added and removed.
    """
    key = progress_key.upper()
    current = await get_progress(db, progress_key=key)
    checked = set(json.loads(current.checked_ids)) if current else set()
    added = sorted({i for i in add if isinstance(i, str)} - checked)
    removed = sorted({i for i in remove if isinstance(i, str)} & checked)
    for case_id in added:
        await case_result_repository.record(
            db, progress_key=key, plan_id=plan_id, case_id=case_id,
            status="passed", actor=actor, source=source,
        )
    for case_id in removed:
        await case_result_repository.record(
            db, progress_key=key, plan_id=plan_id, case_id=case_id,
            status="cleared", actor=actor, source=source,
        )
    return await get_progress(db, progress_key=key), added, removed


async def latest_by_case(
    db: AsyncIOMotorDatabase,
    *,
    progress_key: str,
) -> dict[str, dict[str, Any]]:
    """Who last set each case, and how. Legacy marks name no one.

    Covers every case that has a result or a legacy mark, including results
    that do not count yet (an unapproved agent pass), so a reader can show a
    proposal without mistaking it for a check.
    """
    key = progress_key.upper()
    row = await crud.find_one(db, TestPlanProgress, {"progress_key": key})
    out: dict[str, dict[str, Any]] = {
        case_id: {
            "case_id": case_id,
            "status": "passed",
            "actor": dict(case_result_repository.LEGACY_ACTOR),
            "source": "legacy",
            "plan_id": None,
            "recorded_at": row.updated_at.isoformat() if row and row.updated_at else None,
            "counts": True,
        }
        for case_id in _baseline(row)
    }
    for result in await case_result_repository.list_for_key(db, progress_key=key):
        out[result.case_id] = {
            "case_id": result.case_id,
            "status": result.status,
            "actor": result.actor,
            "source": result.source,
            "plan_id": result.plan_id,
            "recorded_at": result.created_at.isoformat() if result.created_at else None,
            "counts": counts_towards_checked(result),
        }
    return out


async def find_for_same_tickets(
    db: AsyncIOMotorDatabase,
    *,
    progress_key: str,
) -> list[ProgressView]:
    """Every progress view for the ticket(s) in `progress_key`, any shape.

    The key is ``<TICKETS>:<fingerprint>``, and the fingerprint is the plan's
    section sizes — so regenerating a plan into a different shape moves progress
    to a new key and leaves the old one behind. Nothing could see those orphans,
    which is why a ticket with real QA marks renders as 0%: the UI polls the
    current shape's key, gets a 404, and shows an empty checklist that reads as
    "nothing was tested".

    Keys with only results (no legacy row) are included. Returned newest-first,
    including the key asked about if it exists; the caller decides what to do
    with its own shape.
    """
    prefix = progress_key.rsplit(":", 1)[0].upper()
    rows = await crud.find_many(
        db,
        TestPlanProgress,
        {"progress_key": {"$regex": f"^{re.escape(prefix)}:"}},
    )
    keys = {r.progress_key for r in rows}
    keys.update(await case_result_repository.keys_with_prefix(db, prefix=prefix))
    views = [v for v in [await get_progress(db, progress_key=k) for k in keys] if v]
    return sorted(
        views,
        key=lambda v: v.updated_at.timestamp() if v.updated_at else 0.0,
        reverse=True,
    )
