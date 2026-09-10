"""A plan or analysis that already exists must be reusable, not re-bought.

The queue watcher writes a test plan (and, for Bugs, a Bug Lens analysis)
before anyone opens the ticket. That only pays off if the existing artifact
can be read back:

* Test plans could already be read (`GET /plans/{id}`), and the
  Pull-to-Testing auto-generate correctly skips when a run exists — so the
  money was saved, but the plan sat behind the history banner instead of
  being displayed. That half is a frontend fix.
* Bug Lens analyses were persisted from the very first version and had **no
  read path at all**. The watcher paid for an analysis nothing could fetch,
  and the tester paid again by clicking Analyze. That's what
  `GET /bug-lens/by-ticket/{key}` fixes, and what these tests cover.
"""
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.app import bug_lens_routes
from src.app.main import app
from src.app.repositories import bug_analysis_repository

from .conftest import noop_get_sessionmaker

client = TestClient(app)


STORED = {
    "bug_summary": "Loan amount rendered on the buyer estimate.",
    "root_cause": "mapPropertyInfo passed loanAmount into the buyer calculator.",
    "fix_status": "fixed",
    "fix_explanation": "Dropped loanAmount from the buyer mapping.",
    "fix_complexity": None,
    "fix_effort_estimate": None,
    "fix_complexity_reasoning": None,
    "why_tests_miss": "The plan only asserted the field renders when data exists.",
    "is_regression": True,
    "regression_introduced_by": "SK-1898",
    "regression_tests": ["Buyer estimate does not show loan amount"],
    "similar_patterns": ["Shared modals consumed by both roles"],
    "affected_flow": ["Open buyer estimate", "Open property tax modal"],
    "scope_of_impact": ["Buyer estimate"],
    "assumptions": [],
    "open_questions": [],
    "suspect_symbols": ["mapPropertyInfo"],
    "code_evidence": [],
    "suspect_locations": [],
    "blame_evidence": [],
    "run_id": 41,
    "ticket_keys": ["SK-2511"],
    "created_at": "2026-09-08T12:00:00+00:00",
}


def _patch_lookup(return_value):
    """Stub the store lookup *and* the session that reaches it.

    Without the second patch the route opens a real connection to whatever
    DATABASE_URL names — which, until tests/conftest.py started blocking it,
    was production.
    """
    return _both(
        patch.object(
            bug_analysis_repository,
            "find_latest_for_ticket",
            AsyncMock(return_value=return_value),
        ),
        patch.object(bug_lens_routes, "get_sessionmaker", noop_get_sessionmaker),
    )


@contextmanager
def _both(primary, *others):
    """Enter several patches, yielding the first one's mock.

    Callers assert against the lookup mock (`with _patch_lookup(...) as
    mocked`), so the session patch has to ride along without displacing it.
    """
    with primary as mocked:
        with ExitStack() as stack:
            for cm in others:
                stack.enter_context(cm)
            yield mocked


# ---------------------------------------------------------------------------
# GET /bug-lens/by-ticket/{key}
# ---------------------------------------------------------------------------


def test_no_stored_analysis_is_a_200_with_null_not_a_404():
    """Most tickets have no analysis. That's a normal state the UI checks on
    every open — making it an error would put failures in the console for
    ordinary tickets and invite the caller to ignore real failures."""
    with _patch_lookup(None):
        response = client.get("/bug-lens/by-ticket/SK-1")

    assert response.status_code == 200
    body = response.json()
    assert body == {"ticket_key": "SK-1", "analysis": None}


def test_a_stored_analysis_comes_back_in_the_analyze_response_shape():
    """The UI renders one component for a fresh and a stored analysis, so the
    stored one has to arrive in the same shape POST /analyze returns."""
    with _patch_lookup(dict(STORED)):
        response = client.get("/bug-lens/by-ticket/SK-2511")

    assert response.status_code == 200
    body = response.json()
    analysis = body["analysis"]

    assert analysis["ticket_key"] == "SK-2511"
    assert analysis["root_cause"].startswith("mapPropertyInfo")
    assert analysis["regression_tests"] == [
        "Buyer estimate does not show loan amount"
    ]
    # Derived the same way the analyze route derives it.
    assert analysis["is_fixed"] is True
    # Bookkeeping fields belong on the envelope, not inside the analysis, so
    # the payload stays interchangeable with a fresh one.
    for field in ("run_id", "ticket_keys", "created_at"):
        assert field not in analysis
        assert field in body


def test_an_unfixed_bug_is_not_reported_as_fixed():
    with _patch_lookup({**STORED, "fix_status": "not_fixed"}):
        response = client.get("/bug-lens/by-ticket/SK-2511")
    assert response.json()["analysis"]["is_fixed"] is False


def test_the_envelope_carries_when_the_analysis_was_made():
    """The UI needs this to label a stored result as stored — passing a
    week-old analysis off as fresh is worse than not showing it."""
    with _patch_lookup(dict(STORED)):
        body = client.get("/bug-lens/by-ticket/SK-2511").json()
    assert body["created_at"] == "2026-09-08T12:00:00+00:00"
    assert body["run_id"] == 41
    assert body["ticket_keys"] == ["SK-2511"]


def test_the_key_is_normalized():
    with _patch_lookup(None) as mocked:
        client.get("/bug-lens/by-ticket/sk-2511")
    assert mocked.call_args.kwargs["ticket_key"] == "SK-2511"


def test_a_store_failure_is_a_503_not_a_silent_null():
    """Degrading to "no analysis" on a DB error would send the tester off to
    re-run an analysis that already exists — the exact waste this endpoint
    was added to stop."""
    with (
        patch.object(
            bug_analysis_repository,
            "find_latest_for_ticket",
            AsyncMock(side_effect=RuntimeError("connection reset")),
        ),
        patch.object(bug_lens_routes, "get_sessionmaker", noop_get_sessionmaker),
    ):
        response = client.get("/bug-lens/by-ticket/SK-1")
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# The watcher agrees with the UI about what's been analysed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watcher_claims_the_auto_dispatch_flag():
    """The UI's own auto-Bug-Lens checks `auto_bug_analysis_dispatched_at`.
    If the watcher analyses without stamping it, the two auto-fire paths
    disagree about whether the ticket has been analysed."""
    from src.app.services import queue_watcher

    claimed: list[str] = []

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            pass

    async def _mark(session, *, ticket_key):
        claimed.append(ticket_key)

    payload = {
        "ticket_key": "SK-2511",
        "summary": "A bug",
        "description": "body",
        "issue_type": "Bug",
        "development_info": None,
        "comments": None,
        "parent_info": None,
        "linked_info": None,
    }

    with (
        patch.object(
            queue_watcher, "get_sessionmaker", lambda: (lambda: _FakeSession())
        ),
        patch(
            "src.app.repositories.jira_ticket_repository.mark_auto_bug_analysis_dispatched",
            _mark,
        ),
        patch("src.app.bug_lens_routes.analyze_bug", AsyncMock(return_value={})),
    ):
        await queue_watcher._dispatch_bug_lens(payload, {"status": "Ready to Test"})

    assert claimed == ["SK-2511"]


@pytest.mark.asyncio
async def test_watcher_still_analyses_when_the_claim_fails():
    """The claim is bookkeeping. Losing it means a possible duplicate later,
    which is cheaper than skipping the analysis entirely."""
    from src.app.services import queue_watcher

    analyzed = AsyncMock(return_value={})

    with (
        patch.object(
            queue_watcher,
            "get_sessionmaker",
            lambda: (_ for _ in ()).throw(RuntimeError("no db")),
        ),
        patch("src.app.bug_lens_routes.analyze_bug", analyzed),
    ):
        await queue_watcher._dispatch_bug_lens(
            {
                "ticket_key": "SK-2511",
                "summary": "A bug",
                "description": "body",
                "issue_type": "Bug",
            },
            {},
        )

    assert analyzed.await_count == 1


@pytest.mark.asyncio
async def test_a_failed_analysis_does_not_cost_the_plan():
    """Bug Lens runs after the plan is already written and persisted, so an
    analysis failure must stay contained."""
    from src.app.services import queue_watcher

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            pass

    with (
        patch.object(
            queue_watcher, "get_sessionmaker", lambda: (lambda: _FakeSession())
        ),
        patch(
            "src.app.repositories.jira_ticket_repository.mark_auto_bug_analysis_dispatched",
            AsyncMock(),
        ),
        patch(
            "src.app.bug_lens_routes.analyze_bug",
            AsyncMock(side_effect=RuntimeError("llm exploded")),
        ),
    ):
        # Must not raise.
        await queue_watcher._dispatch_bug_lens(
            {
                "ticket_key": "SK-2511",
                "summary": "A bug",
                "description": "body",
                "issue_type": "Bug",
            },
            {},
        )


# ---------------------------------------------------------------------------
# The read path must stay complete as BugAnalysis grows
# ---------------------------------------------------------------------------


def test_every_analysis_field_is_read_back():
    """A field added to BugAnalysis but not to the read query would render
    blank in the UI rather than error — the same silent-degradation shape as
    the missing read path itself. This fails loudly instead."""
    import dataclasses

    from src.app.models import BugAnalysis
    from src.app.repositories.bug_analysis_repository import _ANALYSIS_COLUMNS

    fields = {f.name for f in dataclasses.fields(BugAnalysis)}
    assert fields == set(_ANALYSIS_COLUMNS), (
        "BugAnalysis and the stored-analysis read query have diverged: "
        f"missing from query {sorted(fields - set(_ANALYSIS_COLUMNS))}, "
        f"unknown in query {sorted(set(_ANALYSIS_COLUMNS) - fields)}"
    )
