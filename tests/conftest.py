"""Test-suite guard rails.

Why this file exists
--------------------
``get_sessionmaker()`` resolves ``DATABASE_URL`` lazily, from the environment
or ``.env``, the first time anything asks for a session. Nothing in the test
suite overrode that, so any test that reached ``run_tracker`` — every test
that calls ``plan_service.generate_single``, ``generate_multi`` or a Bug Lens
route — opened a connection to the *production* Neon database and wrote real
rows to it.

That was not theoretical. By 2026-09-10 the production DB held 364 runs whose
only tickets were fixtures (``SK-1``, ``PROJ-1``, ``BUG-10``, …), and three
live tickets carried fake plan versions because the source-grounding tests use
their real keys: SK-2563 was on v10, SK-2700 on v3, SK-2609 on v4, almost all
of them zero-case plans written by a stub LLM in ~1.5s. Those rows show up in
the version history a tester reads, and — worse — they satisfy the watcher's
``has_successful_test_plan`` never-regenerate guard, so a real ticket whose key
a test borrows silently stops getting unattended plans.

Using real ticket keys as fixtures is deliberate here: the tests are named
after the failures they pin down, and the docstrings are the record of what
went wrong. So the fix is not to rename the keys — it is to make it impossible
for a test to reach a real database at all.

How it works
------------
Every test runs with ``init_engine`` replaced by a raiser and the cached
sessionmaker cleared, so the first attempt to get a session fails loudly with
``ProductionDatabaseAccess`` instead of quietly connecting. Callers that
already document graceful degradation (``run_tracker`` returns a
``RunContext(run_id=None)`` and carries on) degrade exactly as they would with
the DB down, which is the behaviour under test anyway.

A test that genuinely needs a database can opt out with
``@pytest.mark.database`` — and is then responsible for pointing at a scratch
DB itself. Nothing does today.
"""
from __future__ import annotations

import pytest

from src.app.db import session as db_session


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
            "DATABASE_URL — it points at production. Stub the repository call, "
            "patch the module's get_sessionmaker, or mark the test "
            "@pytest.mark.database and supply your own scratch DB."
        )

    # get_sessionmaker() reads both of these as module globals at call time, so
    # patching them here reaches every module that did `from ... import
    # get_sessionmaker` at import time.
    monkeypatch.setattr(db_session, "init_engine", _blocked)
    monkeypatch.setattr(db_session, "_sessionmaker", None)
    monkeypatch.setattr(db_session, "engine", None)


class _NoopSession:
    """Satisfies ``async with sessionmaker() as session`` and nothing else.

    For route tests whose repository calls are already stubbed: the session is
    only a handle the route threads through to the repository, so it needs no
    behaviour of its own.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def close(self):
        pass


def noop_get_sessionmaker():
    """Drop-in replacement for a module's imported ``get_sessionmaker``.

    Patch it onto the module under test — ``patch.object(bug_lens_routes,
    "get_sessionmaker", noop_get_sessionmaker)`` — when the route's own
    repository calls are stubbed and only the session plumbing is in the way.
    """
    return lambda: _NoopSession()
