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
from src.app import github_client as gh_client


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


class RealJiraAttachmentAccess(RuntimeError):
    """Raised when a test reaches a real Jira attachment endpoint."""


@pytest.fixture(autouse=True)
def block_real_jira_attachments(monkeypatch: pytest.MonkeyPatch):
    """Keep `post_comment`'s attachment calls off real tickets.

    Many tests build a real ``JiraClient`` and patch only the comment calls,
    and several use live keys (SK-2623, SK-2331). Posting an oversized plan
    uploads its full text as an attachment, and every plan post clears earlier
    copies of that file — a DELETE on a real ticket if left unpatched. These
    fail loudly instead; a test that needs them patches them on its instance,
    which takes precedence over the class attribute set here.
    """
    from src.app.jira_client import JiraClient

    def _blocked(name):
        async def _raise(self, *args, **kwargs):
            raise RealJiraAttachmentAccess(
                f"a test called JiraClient.{name} unpatched — it would reach a real "
                "Jira ticket. Patch it on the client instance."
            )
        return _raise

    for name in (
        "upload_attachments",
        "get_attachments",
        "delete_attachment",
        "download_attachment_text",
    ):
        monkeypatch.setattr(JiraClient, name, _blocked(name))


@pytest.fixture(autouse=True)
def no_desktop_notifications(monkeypatch: pytest.MonkeyPatch):
    """Keep every test off the real macOS notification centre.

    Auto-post alerts and the watcher's token alerts both go through
    ``queue_watcher.notify``, which shells out to terminal-notifier or
    osascript. A test that reaches it unpatched would pop a real banner on
    the developer's screen. Tests that assert on notifications replace
    ``notify`` itself, which this does not affect.
    """
    monkeypatch.setenv("WATCH_NOTIFY", "off")


@pytest.fixture(autouse=True)
def no_watch_schedule(monkeypatch: pytest.MonkeyPatch):
    """Run every test as if the watcher had no active hours.

    ``.env`` may set WATCH_ACTIVE_DAYS / WATCH_ACTIVE_HOURS, and a suite that
    inherited them would pass on a weekday afternoon and fail at night.
    Schedule tests set their own.
    """
    from src.app.config import settings

    monkeypatch.setattr(settings, "watch_active_days", "")
    monkeypatch.setattr(settings, "watch_active_hours", "")


@pytest.fixture(autouse=True)
def reset_code_search_budget():
    """Give every test a fresh GitHub code-search budget.

    The limiter is deliberately process-wide — GitHub bills the quota to the
    token, not to a client object — which means without this the tenth search
    in a *session* starts blocking or raising inside whichever test happens to
    run tenth. Shared rate-limit state is per-process by design and per-test by
    necessity.
    """
    gh_client.reset_code_search_limiter()
    yield
    gh_client.reset_code_search_limiter()


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


@pytest.fixture(autouse=True)
def no_live_repo_evidence(monkeypatch: pytest.MonkeyPatch):
    """Keep plan generation's repo-evidence read off real GitHub.

    ``generate_single`` and ``generate_multi`` list each PR repo's tree and read
    its tests, CI files and Dockerfiles (src/app/evidence_checks.py). With a
    token in ``.env`` that is live traffic from any test whose fixture PR names
    a repository. Returns no evidence by default; tests of the evidence path
    patch ``plan_service._read_repo_evidence`` themselves.
    """
    from src.app.services import plan_service

    async def _none(*_args, **_kwargs):
        return []

    monkeypatch.setattr(plan_service, "_read_repo_evidence", _none)

    async def _nothing_found(*_args, **_kwargs):
        return {"deployments_repos": [], "deploy_sources": {}, "pipelines": {}, "errors": []}

    # Deployments-repo discovery (src/app/build_targets.py) lists trees too.
    monkeypatch.setattr(plan_service, "_discover_deployments", _nothing_found)


@pytest.fixture(autouse=True)
def no_previous_plan(monkeypatch: pytest.MonkeyPatch):
    """Keep regeneration's previous-plan read off the real database.

    ``generate_single`` looks up the ticket's last stored plan and its QA marks
    (``plan_revision``). With ``MONGODB_URI`` in ``.env`` that would read
    whatever plan the fixture's ticket key last got in Atlas. Tests of the
    revision path patch ``plan_service._load_previous_plan`` themselves.
    """
    from src.app.services import plan_service

    async def _none(*_args, **_kwargs):
        return None, None

    monkeypatch.setattr(plan_service, "_load_previous_plan", _none)


@pytest.fixture(autouse=True)
def isolated_figma_probe_cache(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Give every test its own Figma probe cache.

    The real one lives in ~/.jira-testplan-bot and is shared with the running
    app: a test reading it would see yesterday's verdict instead of its mock,
    and a test writing it would tell the live header pill a made-up answer.
    """
    from src.app import token_service

    monkeypatch.setattr(token_service, "FIGMA_PROBE_CACHE_PATH",
                        tmp_path / "figma-probe.json")
