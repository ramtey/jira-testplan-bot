from __future__ import annotations

from typing import Any, TypeVar

from motor.motor_asyncio import AsyncIOMotorClientSession, AsyncIOMotorDatabase

from src.app.db.base import DocumentBase, utcnow
from src.app.db.mongo import next_id

TDoc = TypeVar("TDoc", bound=DocumentBase)


async def insert(
    db: AsyncIOMotorDatabase,
    doc: TDoc,
    *,
    session: AsyncIOMotorClientSession | None = None,
) -> TDoc:
    """Insert `doc`, allocating an integer id, and return it with ``id`` populated.

    Replaces the old ``session.add(...)`` + ``await session.flush()`` pair. Because
    Mongo has no unit of work, the write lands here rather than at a later commit —
    callers that relied on flush to learn the new id get it the same way.
    """
    collection = doc.__collection__
    if doc.id is None:
        doc.id = await next_id(collection, session=session)
    payload = doc.to_doc()
    await db[collection].insert_one(payload, session=session)
    return doc


async def save(
    db: AsyncIOMotorDatabase,
    doc: TDoc,
    *,
    session: AsyncIOMotorClientSession | None = None,
    touch_updated_at: bool = True,
) -> TDoc:
    """Upsert `doc` by its id. Replaces ``session.add(existing)`` on a loaded row.

    ``updated_at`` is refreshed here because Postgres did it via an ``onupdate``
    column default, which has no Mongo equivalent — without this the field would
    silently freeze at insert time.
    """
    if doc.id is None:
        return await insert(db, doc, session=session)
    if touch_updated_at:
        doc.updated_at = utcnow()
    payload = doc.to_doc()
    payload.pop("_id", None)
    await db[doc.__collection__].update_one(
        {"_id": doc.id}, {"$set": payload}, upsert=True, session=session
    )
    return doc


async def get_by_id(
    db: AsyncIOMotorDatabase,
    model: type[TDoc],
    doc_id: int | None,
    *,
    session: AsyncIOMotorClientSession | None = None,
) -> TDoc | None:
    """Replaces ``session.get(Model, id)``."""
    if doc_id is None:
        return None
    doc = await db[model.__collection__].find_one({"_id": doc_id}, session=session)
    return model.from_doc(doc)


async def find_one(
    db: AsyncIOMotorDatabase,
    model: type[TDoc],
    filter_: dict[str, Any],
    *,
    sort: list[tuple[str, int]] | None = None,
    session: AsyncIOMotorClientSession | None = None,
) -> TDoc | None:
    doc = await db[model.__collection__].find_one(filter_, sort=sort, session=session)
    return model.from_doc(doc)


async def find_many(
    db: AsyncIOMotorDatabase,
    model: type[TDoc],
    filter_: dict[str, Any],
    *,
    sort: list[tuple[str, int]] | None = None,
    limit: int | None = None,
    session: AsyncIOMotorClientSession | None = None,
) -> list[TDoc]:
    cursor = db[model.__collection__].find(filter_, session=session)
    if sort:
        cursor = cursor.sort(sort)
    if limit is not None:
        cursor = cursor.limit(limit)
    return [model.from_doc(d) for d in await cursor.to_list(length=limit)]
