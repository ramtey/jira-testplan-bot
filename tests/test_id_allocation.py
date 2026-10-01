"""Integer ids are allocated outside any transaction.

Every insert into a collection bumps the same ``counters`` document. Inside a
transaction that ``$inc`` holds the document until commit, so two plan saves
that overlap — a single-ticket run and a batch run finishing together — collide
on ``counters.generated_plans`` and one aborts with a WriteConflict. On
2026-09-30 that lost SK-2646's regenerated plan: run 982 stayed ``ok`` with no
plan, and the plan reached Jira by editing plan 555's comments in place, so the
bot kept showing 555.

Postgres' ``nextval`` never joined the transaction either; this pins that.
"""

from __future__ import annotations

import pytest

from src.app.db import crud, mongo
from src.app.db.models.plan import PlanTestCase


class _Counters:
    def __init__(self):
        self.calls: list[dict] = []
        self.seq = 0

    async def find_one_and_update(self, filter_, update, **kwargs):
        self.calls.append(kwargs)
        self.seq += 1
        return {"_id": filter_["_id"], "seq": self.seq}


class _Collection:
    def __init__(self):
        self.inserted: list[tuple[dict, object]] = []

    async def insert_one(self, payload, session=None):
        self.inserted.append((payload, session))


class _Db:
    def __init__(self):
        self.counters = _Counters()
        self.collections: dict[str, _Collection] = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, _Collection())


@pytest.mark.asyncio
async def test_insert_in_a_session_allocates_its_id_outside_it(monkeypatch):
    db = _Db()
    monkeypatch.setattr(mongo, "get_db", lambda: db)
    session = object()

    case = await crud.insert(
        db,
        PlanTestCase(plan_id=1, position=0, title="t", body="b"),
        session=session,
    )

    assert case.id == 1
    assert db.counters.calls == [
        {"upsert": True, "return_document": db.counters.calls[0]["return_document"]}
    ]
    assert "session" not in db.counters.calls[0]
    # The document itself still lands inside the transaction.
    [(payload, used_session)] = db[PlanTestCase.__collection__].inserted
    assert used_session is session
    assert payload["_id"] == 1
