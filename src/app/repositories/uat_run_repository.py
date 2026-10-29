from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from src.app.db import crud
from src.app.db.base import utcnow
from src.app.db.models.case_result import CaseResult
from src.app.db.models.uat_run import ACTIVE_STATUSES, UatRun


class ActiveRunExists(Exception):
    """A ticket already has a run that may still record results."""

    def __init__(self, run_id: int | None) -> None:
        super().__init__(f"run {run_id} is active")
        self.run_id = run_id


async def get(db: AsyncIOMotorDatabase, run_id: int) -> UatRun | None:
    return await crud.get_by_id(db, UatRun, run_id)


async def active_for_ticket(db: AsyncIOMotorDatabase, *, ticket_key: str) -> UatRun | None:
    return await crud.find_one(db, UatRun, {"active_ticket": ticket_key.upper()})


async def list_for_ticket(db: AsyncIOMotorDatabase, *, ticket_key: str, limit: int = 20) -> list[UatRun]:
    return await crud.find_many(
        db, UatRun, {"ticket_key": ticket_key.upper()}, sort=[("_id", -1)], limit=limit
    )


async def create(db: AsyncIOMotorDatabase, run: UatRun) -> UatRun:
    """Insert `run` as the ticket's one active run.

    The check before the insert gives a readable refusal; the unique index on
    ``active_ticket`` is what actually holds when two starts race.
    """
    run.ticket_key = run.ticket_key.upper()
    run.active_ticket = run.ticket_key if run.status in ACTIVE_STATUSES else None
    existing = await active_for_ticket(db, ticket_key=run.ticket_key)
    if existing is not None:
        raise ActiveRunExists(existing.id)
    try:
        return await crud.insert(db, run)
    except DuplicateKeyError:
        existing = await active_for_ticket(db, ticket_key=run.ticket_key)
        raise ActiveRunExists(existing.id if existing else None)


async def save(db: AsyncIOMotorDatabase, run: UatRun) -> UatRun:
    """Persist `run`, releasing the ticket once it is no longer active."""
    run.active_ticket = run.ticket_key if run.status in ACTIVE_STATUSES else None
    if run.active_ticket is None and run.finished_at is None:
        run.finished_at = utcnow()
    return await crud.save(db, run)


async def update_leg(db: AsyncIOMotorDatabase, run_id: int, leg: str, fields: dict[str, Any]) -> None:
    """Set fields on one leg without rewriting the others.

    Legs run as separate processes and report on their own; a whole-document
    save from one would undo what another just wrote.
    """
    await db[UatRun.__collection__].update_one(
        {"_id": run_id},
        {"$set": {**{f"legs.{leg}.{k}": v for k, v in fields.items()}, "updated_at": utcnow()}},
    )


async def results_for_run(db: AsyncIOMotorDatabase, run_id: int) -> list[CaseResult]:
    """Every result recorded on `run_id`, oldest first."""
    return await crud.find_many(db, CaseResult, {"run_id": run_id}, sort=[("_id", 1)])


async def result_by_client_id(db: AsyncIOMotorDatabase, client_id: str) -> CaseResult | None:
    return await crud.find_one(db, CaseResult, {"client_id": client_id})
