"""Test-suite guard rails.

Why this file exists
--------------------
``get_db()`` resolves ``MONGODB_URI`` lazily, from the environment or ``.env``,
the first time anything asks for the database. Nothing in the test suite
overrode that, so any test that reached ``run_tracker`` — every test that calls
``plan_service.generate_single``, ``generate_multi`` or a Bug Lens route —
opened a connection to the *production* database and wrote real records to it.

That was not theoretical. By 2026-09-10 the production DB held 364 runs whose
only tickets were fixtures (``SK-1``, ``PROJ-1``, ``BUG-10``, …), and three
live tickets carried fake plan versions because the source-grounding tests use
their real keys: SK-2563 was on v10, SK-2700 on v3, SK-2609 on v4, almost all
of them zero-case plans written by a stub LLM in ~1.5s. Those records show up
in the version history a tester reads, and — worse — they satisfy the watcher's
``has_successful_test_plan`` never-regenerate guard, so a real ticket whose key
a test borrows silently stops getting unattended plans.

Using real ticket keys as fixtures is deliberate here: the tests are named
after the failures they pin down, and the docstrings are the record of what
went wrong. So the fix is not to rename the keys — it is to make it impossible
for a test to reach a real database at all.

How it works
------------
Every test runs with ``init_client`` replaced by a raiser and the cached client
cleared, so the first attempt to get the database fails loudly with
``ProductionDatabaseAccess`` instead of quietly connecting. Callers that
already document graceful degradation (``run_tracker`` returns a
``RunContext(run_id=None)`` and carries on) degrade exactly as they would with
the DB down, which is the behaviour under test anyway.

Note that Motor connects lazily — constructing a client performs no I/O — so
without this guard a misconfigured test would not fail at connect time but at
first *write*, which is far too late. Blocking ``init_client`` is what makes
the failure immediate.

A test that genuinely needs a database can opt out with
``@pytest.mark.database`` — and is then responsible for pointing at a scratch
database itself. Nothing does today.
"""
from __future__ import annotations

import pytest

from src.app.db import mongo as db_mongo


class ProductionDatabaseAccess(RuntimeError):
    """Raised when a test tries to open a real database connection."""


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "database: test may open a real database connection (opts out of the "
        "conftest guard; must point at a scratch DB itself)",
    )


@pytest.fixture(autouse=True)
def block_real_database(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if request.node.get_closest_marker("database"):
        return

    def _blocked(*args, **kwargs):
        raise ProductionDatabaseAccess(
            "a test tried to open a database connection. Tests must not touch "
            "MONGODB_URI — it points at a shared cluster. Stub the repository "
            "call, patch the module's get_db, or mark the test "
            "@pytest.mark.database and supply your own scratch DB."
        )

    # get_db() reads these as module globals at call time, so patching them here
    # reaches every module that did `from ... import get_db` at import time.
    monkeypatch.setattr(db_mongo, "init_client", _blocked)
    monkeypatch.setattr(db_mongo, "_db", None)
    monkeypatch.setattr(db_mongo, "_client", None)


class _NoopDatabase:
    """Stands in for the Motor database handle and does nothing.

    For route tests whose repository calls are already stubbed: the handle is
    only something the route threads through to the repository, so it needs no
    behaviour of its own. Indexing it (``db["runs"]``) returns another no-op so
    an accidental collection access fails on use rather than on lookup.
    """

    def __getitem__(self, _name):
        return self

    def __getattr__(self, _name):
        return self


def noop_get_db():
    """Drop-in replacement for a module's imported ``get_db``.

    Patch it onto the module under test — ``patch.object(bug_lens_routes,
    "get_db", noop_get_db)`` — when the route's own repository calls are stubbed
    and only the database plumbing is in the way.
    """
    return _NoopDatabase()
