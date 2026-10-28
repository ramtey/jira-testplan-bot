from __future__ import annotations

import re
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.app.config import settings
from src.app.db import crud
from src.app.db.models.case_result import CaseResult
from src.app.repositories import user_repository

LEGACY_ACTOR: dict[str, Any] = {
    "kind": "legacy_unknown",
    "user_id": None,
    "email": None,
    "name": None,
}


async def human_actor(db: AsyncIOMotorDatabase) -> dict[str, Any]:
    """The person using this app, as an actor.

    There is no login, so the person is whoever ``QA_ACTOR_EMAIL`` names. When
    it is unset the actor is still recorded as human (a click in the browser is
    a person), with no identity — and ``email: None`` says so rather than
    borrowing the Jira service account's address, which is a robot's.
    """
    email = (settings.qa_actor_email or "").strip()
    if not email:
        return {"kind": "human", "user_id": None, "email": None, "name": None}
    user = await user_repository.get_or_create_by_email(db, email=email)
    return {
        "kind": "human",
        "user_id": user.id,
        "email": user.email,
        "name": user.display_name,
    }


async def record(
    db: AsyncIOMotorDatabase,
    *,
    progress_key: str,
    plan_id: int | None,
    case_id: str,
    status: str,
    actor: dict[str, Any],
    source: str,
    **extra: Any,
) -> CaseResult:
    """Append one result. Never updates an existing row."""
    row = CaseResult(
        progress_key=progress_key.upper(),
        plan_id=plan_id,
        case_id=case_id,
        status=status,
        actor=actor,
        source=source,
        **extra,
    )
    return await crud.insert(db, row)


async def list_for_key(db: AsyncIOMotorDatabase, *, progress_key: str) -> list[CaseResult]:
    """Every result filed under `progress_key`, oldest first.

    Ordered by id, which is allocated when the row is written, so the fold in
    `test_plan_progress_repository` applies results in the order they happened.
    """
    return await crud.find_many(
        db, CaseResult, {"progress_key": progress_key.upper()}, sort=[("_id", 1)]
    )


async def keys_with_prefix(db: AsyncIOMotorDatabase, *, prefix: str) -> list[str]:
    """Distinct progress keys with results, for the ticket(s) `prefix` names."""
    keys = await db[CaseResult.__collection__].distinct(
        "progress_key", {"progress_key": {"$regex": f"^{re.escape(prefix.upper())}:"}}
    )
    return [k for k in keys if isinstance(k, str)]
