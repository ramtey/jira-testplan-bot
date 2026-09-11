from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorClientSession,
    AsyncIOMotorDatabase,
)
from pymongo import ASCENDING, DESCENDING, ReturnDocument

DEFAULT_DB_NAME = "jira_testplan_bot"

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None
_supports_transactions: bool | None = None


def _resolve_mongo_uri(override: str | None = None) -> str:
    if override:
        return override
    env_val = os.environ.get("MONGODB_URI")
    if env_val:
        return env_val
    # Fall back to pydantic-settings (auto-loads .env) so imports that don't read
    # env directly still work. Imported lazily to avoid a module-load cycle.
    from src.app.config import settings

    if getattr(settings, "mongodb_uri", None):
        return settings.mongodb_uri
    raise RuntimeError(
        "MONGODB_URI is not set — configure it in .env or pass it to init_client()"
    )


def _resolve_db_name(uri: str, override: str | None = None) -> str:
    if override:
        return override
    env_val = os.environ.get("MONGODB_DB")
    if env_val:
        return env_val
    # mongodb+srv://user:pw@host/<dbname>?opts — use the path segment when present.
    from urllib.parse import urlsplit

    path = urlsplit(uri).path.lstrip("/")
    return path or DEFAULT_DB_NAME


def init_client(
    mongodb_uri: str | None = None, db_name: str | None = None
) -> AsyncIOMotorDatabase:
    global _client, _db, _supports_transactions
    uri = _resolve_mongo_uri(mongodb_uri)
    _client = AsyncIOMotorClient(
        uri,
        maxPoolSize=15,
        serverSelectionTimeoutMS=10_000,
        tz_aware=True,
    )
    _db = _client[_resolve_db_name(uri, db_name)]
    _supports_transactions = None
    return _db


def get_db() -> AsyncIOMotorDatabase:
    """The Mongo database handle. Replaces the old ``get_sessionmaker()``.

    Repositories take this directly instead of a SQLAlchemy session: Mongo has no
    session-scoped identity map or unit of work, so every repository call writes
    immediately. Callers that used to end a block with ``await session.commit()``
    no longer need to — there is nothing buffered to flush.
    """
    if _db is None:
        init_client()
    assert _db is not None
    return _db


def get_client() -> AsyncIOMotorClient:
    if _client is None:
        init_client()
    assert _client is not None
    return _client


def reset_client() -> None:
    """Drop the cached client/db. Used by tests that repoint at a scratch database."""
    global _client, _db, _supports_transactions
    if _client is not None:
        _client.close()
    _client = None
    _db = None
    _supports_transactions = None


async def supports_transactions() -> bool:
    """Whether the connected deployment can run multi-document transactions.

    True on a replica set or sharded cluster (Atlas always qualifies), False on a
    standalone ``mongod``. Cached after the first probe. Callers use this to skip
    the transaction wrapper rather than crash against a standalone dev instance.
    """
    global _supports_transactions
    if _supports_transactions is None:
        try:
            hello = await get_client().admin.command("hello")
            _supports_transactions = bool(
                hello.get("setName") or hello.get("msg") == "isdbgrid"
            )
        except Exception:
            _supports_transactions = False
    return _supports_transactions


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncIOMotorClientSession | None]:
    """Run a block atomically when the deployment supports it.

    Yields a session to pass to writes, or ``None`` on a standalone deployment
    where each write simply lands on its own. Degrading to None is deliberate and
    loud in one place rather than silently per-call: a standalone dev Mongo stays
    usable, while Atlas (a replica set) gets real atomicity.
    """
    if not await supports_transactions():
        yield None
        return
    async with await get_client().start_session() as session:
        async with session.start_transaction():
            yield session


async def next_id(collection: str, session: AsyncIOMotorClientSession | None = None) -> int:
    """Allocate the next integer id for `collection`.

    The app's ids are integers end to end — API routes, the frontend's URLs, and
    ``generated_plans.previous_plan_id`` chains all pass them around as ints. Using
    ObjectIds would have broken every one of those contracts and made the Neon
    backfill a renumbering exercise, so this reproduces Postgres' SERIAL with an
    atomic ``$inc`` on a counters document instead.
    """
    doc = await get_db().counters.find_one_and_update(
        {"_id": collection},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
        session=session,
    )
    return int(doc["seq"])


async def sync_counter(collection: str, highest_existing_id: int) -> None:
    """Raise `collection`'s counter to at least `highest_existing_id`.

    Called by the Neon backfill, which inserts documents with their original
    Postgres ids rather than allocating new ones. Without this the first
    post-migration insert would reuse id 1 and collide with migrated data.
    """
    await get_db().counters.update_one(
        {"_id": collection},
        {"$max": {"seq": int(highest_existing_id)}},
        upsert=True,
    )


# Indexes mirroring the constraints the Alembic migrations used to enforce.
# Unique indexes are the load-bearing ones: several repositories rely on
# "one row per ticket_key" to make their upserts correct.
_INDEX_SPEC: dict[str, list[dict[str, Any]]] = {
    "users": [
        {"keys": [("email", ASCENDING)], "unique": True, "name": "uq_users_email"},
        {"keys": [("jira_account_id", ASCENDING)], "name": "ix_users_jira_account_id"},
    ],
    "jira_tickets": [
        {"keys": [("ticket_key", ASCENDING)], "unique": True, "name": "uq_jira_tickets_key"},
        {"keys": [("project_key", ASCENDING)], "name": "ix_jira_tickets_project"},
        {"keys": [("parent_key", ASCENDING)], "name": "ix_jira_tickets_parent"},
    ],
    "runs": [
        {"keys": [("ticket_keys", ASCENDING)], "name": "ix_runs_ticket_keys"},
        {"keys": [("created_at", DESCENDING)], "name": "ix_runs_created_at_desc"},
        {"keys": [("user_id", ASCENDING), ("created_at", DESCENDING)], "name": "ix_runs_user_created_at"},
        {"keys": [("run_type", ASCENDING)], "name": "ix_runs_run_type"},
        {"keys": [("status", ASCENDING)], "name": "ix_runs_status"},
    ],
    "generated_plans": [
        {"keys": [("run_id", ASCENDING)], "name": "ix_generated_plans_run"},
        {"keys": [("created_at", DESCENDING)], "name": "ix_generated_plans_created_at"},
        {"keys": [("previous_plan_id", ASCENDING)], "name": "ix_generated_plans_previous"},
    ],
    "plan_test_cases": [
        {"keys": [("plan_id", ASCENDING), ("position", ASCENDING)], "name": "ix_plan_test_cases_plan_position"},
        {"keys": [("category", ASCENDING)], "name": "ix_plan_test_cases_category"},
    ],
    "bug_analyses": [
        {"keys": [("run_id", ASCENDING)], "name": "ix_bug_analyses_run"},
    ],
    "feedback_events": [
        {"keys": [("target_type", ASCENDING), ("target_id", ASCENDING)], "name": "ix_feedback_events_target"},
        {"keys": [("user_id", ASCENDING)], "name": "ix_feedback_events_user"},
        {"keys": [("created_at", DESCENDING)], "name": "ix_feedback_events_created_at"},
    ],
    "ticket_walkthroughs": [
        {"keys": [("ticket_key", ASCENDING)], "unique": True, "name": "uq_ticket_walkthroughs_key"},
    ],
    "test_plan_progress": [
        {"keys": [("progress_key", ASCENDING)], "unique": True, "name": "uq_test_plan_progress_key"},
    ],
    "ticket_holds": [
        {"keys": [("ticket_key", ASCENDING)], "unique": True, "name": "uq_ticket_holds_key"},
    ],
}


async def ensure_indexes() -> None:
    """Create every index the app relies on. Idempotent, so it is safe to call on
    each startup — this is what replaces ``alembic upgrade head``."""
    db = get_db()
    for collection, specs in _INDEX_SPEC.items():
        for spec in specs:
            kwargs = {k: v for k, v in spec.items() if k != "keys"}
            await db[collection].create_index(spec["keys"], **kwargs)
