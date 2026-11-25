"""The posting lease against a real (mock) Mongo: the unique `_id` is what
stops two processes posting one ticket, so it has to be exercised, not mocked."""
from datetime import timedelta

import pytest
from mongomock_motor import AsyncMongoMockClient

from src.app.db.base import utcnow
from src.app.repositories import auto_post_repository as repo

pytestmark = pytest.mark.asyncio


@pytest.fixture
def db():
    return AsyncMongoMockClient()["testplan_test"]


async def test_only_one_holder_at_a_time(db):
    assert await repo.claim(db, ticket_key="sk-1", owner="watcher", lease_seconds=300)
    assert not await repo.claim(db, ticket_key="SK-1", owner="api", lease_seconds=300)
    # Other tickets are unaffected.
    assert await repo.claim(db, ticket_key="SK-2", owner="api", lease_seconds=300)


async def test_a_released_ticket_can_be_claimed_again(db):
    assert await repo.claim(db, ticket_key="SK-1", owner="watcher", lease_seconds=300)
    await repo.release(db, ticket_key="SK-1", owner="watcher")
    assert await repo.claim(db, ticket_key="SK-1", owner="api", lease_seconds=300)


async def test_only_the_holder_can_release(db):
    assert await repo.claim(db, ticket_key="SK-1", owner="watcher", lease_seconds=300)
    await repo.release(db, ticket_key="SK-1", owner="api")
    assert not await repo.claim(db, ticket_key="SK-1", owner="api", lease_seconds=300)


async def test_a_dead_holders_lease_runs_out(db):
    assert await repo.claim(db, ticket_key="SK-1", owner="crashed", lease_seconds=300)
    await db[repo.CLAIMS].update_one(
        {"_id": "SK-1"}, {"$set": {"expires_at": utcnow() - timedelta(seconds=1)}}
    )
    assert await repo.claim(db, ticket_key="SK-1", owner="api", lease_seconds=300)
    doc = await db[repo.CLAIMS].find_one({"_id": "SK-1"})
    assert doc["owner"] == "api"
    # The crashed holder waking up late cannot drop the new holder's lease.
    await repo.release(db, ticket_key="SK-1", owner="crashed")
    assert await db[repo.CLAIMS].find_one({"_id": "SK-1"}) is not None
