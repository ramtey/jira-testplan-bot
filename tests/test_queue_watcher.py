"""Guard tests for the QA-queue watcher.

The watcher spends real Opus calls with nobody watching, so what's pinned
here is mostly the *refusals*: every guard that stops a ticket short of the
LLM, and the order they run in (cheapest first). A regression that quietly
disables one of these doesn't fail loudly — it just costs money, so it needs
a test rather than a comment.
"""
from unittest.mock import AsyncMock, patch

import pytest

from src.app.config import settings
from src.app.models import EpicChildSummary
from src.app.services import queue_watcher


def _rows(*keys) -> list[EpicChildSummary]:
    return [
        EpicChildSummary(key=k, summary=f"{k} summary", issue_type="Story")
        for k in keys
    ]


def _merged_pr():
    return {"url": "pr/1", "status": "MERGED"}


def _payload(*, pull_requests=None, issue_type="Story") -> dict:
    """A prompt payload as plan_service would build it."""
    return {
        "ticket_key": "SK-1",
        "summary": "A ticket",
        "description": "body",
        "issue_type": issue_type,
        "testing_context": {},
        "development_info": {"pull_requests": pull_requests or []},
        "image_urls": None,
        "comments": None,
        "parent_info": None,
        "child_info": None,
        "linked_info": None,
        "bounce_history": None,
    }


class _Harness:
    """Patches the watcher's four collaborators: the board, the dedupe
    screen, the Jira fetch, and generation."""

    def __init__(
        self,
        *,
        queue=("SK-1",),
        screen=None,
        payload=None,
        plan=None,
        generate_error=None,
    ):
        self.queue = list(queue)
        self.screen = screen or {}
        self.payload = payload or _payload(pull_requests=[_merged_pr()])
        self.plan = plan or {"happy_path": [{"title": "t"}], "plan_id": 7}
        self.generate_error = generate_error
        self.generated: list[str] = []
        self.bug_lens_calls: list[str] = []

    def __enter__(self):
        async def _search(_self, project_key, status_name):
            return _rows(*self.queue)

        async def _screen(ticket_key):
            return self.screen.get(ticket_key)

        async def _generate(request, **kwargs):
            if self.generate_error:
                raise self.generate_error
            self.generated.append(request.ticket_key)
            return self.plan

        async def _bug_lens(payload, serialized):
            self.bug_lens_calls.append(payload["ticket_key"])

        def _prompt_payload(_serialized):
            return {**self.payload, "ticket_key": _serialized["key"]}

        self._patches = [
            patch.object(
                queue_watcher.JiraClient, "search_project_issues", _search
            ),
            patch.object(
                queue_watcher.JiraClient,
                "get_issue",
                AsyncMock(side_effect=lambda key: _FakeIssue(key)),
            ),
            patch.object(queue_watcher, "_screen", _screen),
            patch.object(
                queue_watcher.plan_service,
                "serialize_issue",
                lambda issue: {"key": issue.key, "status": "Ready to Test",
                               "status_category": "indeterminate"},
            ),
            patch.object(queue_watcher.plan_service, "prompt_payload", _prompt_payload),
            patch.object(queue_watcher.plan_service, "generate_single", _generate),
            patch.object(queue_watcher, "_dispatch_bug_lens", _bug_lens),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


class _FakeIssue:
    def __init__(self, key):
        self.key = key


def _reasons(result) -> dict[str, str | None]:
    return {o.ticket_key: o.reason for o in result.outcomes}


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generates_for_a_fresh_ticket_with_a_pr():
    with _Harness() as h:
        result = await queue_watcher.sweep_once(projects=["SK"])
    assert h.generated == ["SK-1"]
    assert [o.action for o in result.outcomes] == ["generated"]
    assert result.scanned == 1


@pytest.mark.asyncio
async def test_skips_a_ticket_that_already_has_a_plan():
    """Never regenerate unattended — a second version is a human decision."""
    with _Harness(screen={"SK-1": queue_watcher.SKIP_ALREADY_PLANNED}) as h:
        result = await queue_watcher.sweep_once(projects=["SK"])
    assert h.generated == []
    assert _reasons(result) == {"SK-1": queue_watcher.SKIP_ALREADY_PLANNED}


@pytest.mark.asyncio
async def test_skips_a_ticket_attempted_within_the_cooldown():
    """A ticket that fails every sweep would otherwise burn a call per cycle."""
    with _Harness(screen={"SK-1": queue_watcher.SKIP_COOLDOWN}) as h:
        result = await queue_watcher.sweep_once(projects=["SK"])
    assert h.generated == []
    assert _reasons(result) == {"SK-1": queue_watcher.SKIP_COOLDOWN}


@pytest.mark.asyncio
async def test_skips_a_ticket_with_no_pr_at_all(monkeypatch):
    monkeypatch.setattr(settings, "watch_require_merged_pr", True, raising=False)
    with _Harness(payload=_payload(pull_requests=[])) as h:
        result = await queue_watcher.sweep_once(projects=["SK"])
    assert h.generated == []
    assert _reasons(result) == {"SK-1": queue_watcher.SKIP_NO_MERGED_PR}


@pytest.mark.asyncio
async def test_skips_a_ticket_whose_pr_is_still_open(monkeypatch):
    """The permanence trap: the watcher never regenerates, so a plan written
    against an open PR outlives the code it describes."""
    monkeypatch.setattr(settings, "watch_require_merged_pr", True, raising=False)
    with _Harness(payload=_payload(pull_requests=[{"url": "pr/1", "status": "OPEN"}])) as h:
        result = await queue_watcher.sweep_once(projects=["SK"])
    assert h.generated == []
    assert _reasons(result) == {"SK-1": queue_watcher.SKIP_NO_MERGED_PR}


@pytest.mark.asyncio
async def test_skips_a_ticket_whose_only_pr_was_declined(monkeypatch):
    """A declined PR describes code that will never ship."""
    monkeypatch.setattr(settings, "watch_require_merged_pr", True, raising=False)
    with _Harness(
        payload=_payload(pull_requests=[{"url": "pr/1", "status": "DECLINED"}])
    ) as h:
        result = await queue_watcher.sweep_once(projects=["SK"])
    assert h.generated == []
    assert _reasons(result) == {"SK-1": queue_watcher.SKIP_NO_MERGED_PR}


@pytest.mark.asyncio
async def test_one_merged_pr_among_others_is_enough(monkeypatch):
    """Real tickets carry a declined first attempt alongside the merged fix —
    SK-2563 in the live queue looks exactly like this."""
    monkeypatch.setattr(settings, "watch_require_merged_pr", True, raising=False)
    with _Harness(
        payload=_payload(
            pull_requests=[
                {"url": "pr/1", "status": "DECLINED"},
                {"url": "pr/2", "status": "merged"},  # case-insensitive
            ]
        )
    ) as h:
        await queue_watcher.sweep_once(projects=["SK"])
    assert h.generated == ["SK-1"]


@pytest.mark.asyncio
async def test_pr_gate_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(settings, "watch_require_merged_pr", False, raising=False)
    with _Harness(payload=_payload(pull_requests=[])) as h:
        await queue_watcher.sweep_once(projects=["SK"])
    assert h.generated == ["SK-1"]


@pytest.mark.asyncio
async def test_a_generator_refusal_is_reported_as_skipped(monkeypatch):
    """With the merged-PR gate off, the generator can still refuse a ticket
    it finds no source for. Recording that as "generated, 0 cases" would
    read as a degraded plan rather than as a deliberate refusal."""
    monkeypatch.setattr(settings, "watch_require_merged_pr", False, raising=False)
    with _Harness(
        payload=_payload(pull_requests=[]),
        plan={"no_source": True, "happy_path": [], "searched": ["PRs", "branches"]},
    ) as h:
        result = await queue_watcher.sweep_once(projects=["SK"])
    assert h.generated == ["SK-1"], "the generator was called and declined"
    assert _reasons(result) == {"SK-1": queue_watcher.SKIP_NO_SOURCE}
    assert result.generated == []


@pytest.mark.asyncio
async def test_skips_a_ticket_a_qa_put_on_hold():
    """A hold means a human parked the ticket. `code-review` means the PR is
    still in review, so a plan written now describes code that will change —
    and never-regenerate would make it permanent."""
    with _Harness(screen={"SK-1": queue_watcher.SKIP_ON_HOLD}) as h:
        result = await queue_watcher.sweep_once(projects=["SK"])
    assert h.generated == []
    assert _reasons(result) == {"SK-1": queue_watcher.SKIP_ON_HOLD}


@pytest.mark.asyncio
async def test_stops_at_the_per_sweep_cap():
    """Bounds the bill when a sprint's worth of tickets moves at once."""
    with _Harness(queue=("SK-1", "SK-2", "SK-3", "SK-4")) as h:
        result = await queue_watcher.sweep_once(projects=["SK"], max_per_cycle=2)
    assert len(h.generated) == 2
    capped = [
        o for o in result.outcomes if o.reason == queue_watcher.SKIP_CAP_REACHED
    ]
    assert len(capped) == 2


@pytest.mark.asyncio
async def test_dry_run_reaches_every_guard_but_never_the_llm():
    """--dry-run has to be honest: same guards, same verdicts, no spend."""
    with _Harness(queue=("SK-1", "SK-2"), screen={"SK-2": queue_watcher.SKIP_ALREADY_PLANNED}) as h:
        result = await queue_watcher.sweep_once(projects=["SK"], dry_run=True)
    assert h.generated == []
    assert _reasons(result) == {"SK-1": "dry_run", "SK-2": queue_watcher.SKIP_ALREADY_PLANNED}


# ---------------------------------------------------------------------------
# Failure containment — one bad ticket must not end the sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_generation_failure_does_not_stop_the_sweep():
    class _Boom(Exception):
        pass

    with _Harness(queue=("SK-1", "SK-2"), generate_error=_Boom("model exploded")) as h:
        result = await queue_watcher.sweep_once(projects=["SK"])
    assert h.generated == []
    assert [o.action for o in result.outcomes] == ["failed", "failed"]
    assert "model exploded" in result.outcomes[0].detail


@pytest.mark.asyncio
async def test_a_non_testable_ticket_is_a_skip_not_a_failure():
    with _Harness(
        generate_error=queue_watcher.plan_service.NonTestableIssueError("Epic")
    ) as h:
        result = await queue_watcher.sweep_once(projects=["SK"])
    assert h.generated == []
    assert _reasons(result) == {"SK-1": queue_watcher.SKIP_NON_TESTABLE}


@pytest.mark.asyncio
async def test_an_unreadable_queue_is_reported_not_swallowed():
    """An empty result and an unreachable Jira must not look the same — a
    silent 'nothing to do' would hide an expired token indefinitely."""
    from src.app.jira_client import JiraAuthError

    async def _boom(_self, project_key, status_name):
        raise JiraAuthError("token expired", status_code=401)

    with patch.object(queue_watcher.JiraClient, "search_project_issues", _boom):
        result = await queue_watcher.sweep_once(projects=["SK"])

    assert result.queue_error is not None
    assert "token expired" in result.queue_error
    assert result.outcomes == []


@pytest.mark.asyncio
async def test_one_unreadable_project_does_not_block_the_others():
    from src.app.jira_client import JiraConnectionError

    async def _half_broken(_self, project_key, status_name):
        if project_key == "BAD":
            raise JiraConnectionError("unreachable")
        return _rows("SK-1")

    with _Harness() as h:
        with patch.object(
            queue_watcher.JiraClient, "search_project_issues", _half_broken
        ):
            result = await queue_watcher.sweep_once(projects=["BAD", "SK"])

    assert h.generated == ["SK-1"]
    assert result.queue_error is not None


# ---------------------------------------------------------------------------
# Bug Lens hand-off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug_tickets_also_get_bug_lens():
    with _Harness(
        payload=_payload(pull_requests=[_merged_pr()], issue_type="Bug")
    ) as h:
        await queue_watcher.sweep_once(projects=["SK"])
    assert h.generated == ["SK-1"]
    assert h.bug_lens_calls == ["SK-1"]


@pytest.mark.asyncio
async def test_non_bug_tickets_do_not_get_bug_lens():
    with _Harness() as h:
        await queue_watcher.sweep_once(projects=["SK"])
    assert h.bug_lens_calls == []


@pytest.mark.asyncio
async def test_dry_run_does_not_dispatch_bug_lens():
    with _Harness(
        payload=_payload(pull_requests=[_merged_pr()], issue_type="Bug")
    ) as h:
        await queue_watcher.sweep_once(projects=["SK"], dry_run=True)
    assert h.bug_lens_calls == []


# ---------------------------------------------------------------------------
# Project resolution
# ---------------------------------------------------------------------------


def test_watched_projects_falls_back_to_the_workflow_prefixes(monkeypatch):
    """One place to name the team's projects — a team that configured the QA
    workflow buttons shouldn't have to repeat itself for the watcher."""
    monkeypatch.setattr(settings, "watch_projects", [], raising=False)
    monkeypatch.setattr(settings, "workflow_project_prefixes", ["SK", "sl"], raising=False)
    assert queue_watcher.watched_projects() == ["SK", "SL"]


def test_explicit_watch_projects_win(monkeypatch):
    monkeypatch.setattr(settings, "watch_projects", ["qa"], raising=False)
    monkeypatch.setattr(settings, "workflow_project_prefixes", ["SK"], raising=False)
    assert queue_watcher.watched_projects() == ["QA"]


# ---------------------------------------------------------------------------
# _screen — the DB-only guard the sweep runs before spending a Jira fetch
# ---------------------------------------------------------------------------


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_screen_db(*, has_plan: bool, last_attempt, hold=None):
    """Patch the three repository reads _screen makes, plus the sessionmaker."""
    return [
        patch.object(queue_watcher, "get_sessionmaker", lambda: (lambda: _FakeSession())),
        patch.object(
            queue_watcher.plan_repository,
            "has_successful_test_plan",
            AsyncMock(return_value=has_plan),
        ),
        patch.object(
            queue_watcher.ticket_hold_repository,
            "get_hold",
            AsyncMock(return_value=hold),
        ),
        patch.object(
            queue_watcher.plan_repository,
            "find_last_test_plan_attempt_at",
            AsyncMock(return_value=last_attempt),
        ),
    ]


@pytest.mark.asyncio
async def test_screen_passes_a_ticket_with_no_history():
    for p in _patch_screen_db(has_plan=False, last_attempt=None):
        p.start()
    try:
        assert await queue_watcher._screen("SK-1") is None
    finally:
        patch.stopall()


@pytest.mark.asyncio
async def test_screen_blocks_a_ticket_with_a_stored_plan():
    for p in _patch_screen_db(has_plan=True, last_attempt=None):
        p.start()
    try:
        assert await queue_watcher._screen("SK-1") == queue_watcher.SKIP_ALREADY_PLANNED
    finally:
        patch.stopall()


@pytest.mark.asyncio
async def test_screen_blocks_a_recent_failed_attempt(monkeypatch):
    """A failed run leaves no plan row, so only the cooldown stops the
    retry loop — this is the guard that keeps a reliably-failing ticket
    from costing a call every interval."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(settings, "watch_retry_cooldown_hours", 6, raising=False)
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    for p in _patch_screen_db(has_plan=False, last_attempt=recent):
        p.start()
    try:
        assert await queue_watcher._screen("SK-1") == queue_watcher.SKIP_COOLDOWN
    finally:
        patch.stopall()


@pytest.mark.asyncio
async def test_screen_retries_once_the_cooldown_has_passed(monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(settings, "watch_retry_cooldown_hours", 6, raising=False)
    stale = datetime.now(timezone.utc) - timedelta(hours=7)
    for p in _patch_screen_db(has_plan=False, last_attempt=stale):
        p.start()
    try:
        assert await queue_watcher._screen("SK-1") is None
    finally:
        patch.stopall()


@pytest.mark.asyncio
async def test_screen_treats_a_naive_timestamp_as_utc(monkeypatch):
    """Whether created_at comes back tz-aware depends on the driver; a naive
    one must not crash the sweep with a subtraction TypeError."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(settings, "watch_retry_cooldown_hours", 6, raising=False)
    naive_recent = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)
    for p in _patch_screen_db(has_plan=False, last_attempt=naive_recent):
        p.start()
    try:
        assert await queue_watcher._screen("SK-1") == queue_watcher.SKIP_COOLDOWN
    finally:
        patch.stopall()


@pytest.mark.asyncio
async def test_screen_cooldown_can_be_disabled(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(settings, "watch_retry_cooldown_hours", 0, raising=False)
    for p in _patch_screen_db(has_plan=False, last_attempt=datetime.now(timezone.utc)):
        p.start()
    try:
        assert await queue_watcher._screen("SK-1") is None
    finally:
        patch.stopall()


@pytest.mark.asyncio
async def test_screen_blocks_a_held_ticket():
    """The interaction that motivated this guard: a ticket held for
    `code-review` is in the QA queue but its PR is still changing, and the
    never-regenerate rule would freeze whatever plan got written now."""
    for p in _patch_screen_db(
        has_plan=False, last_attempt=None, hold=object()
    ):
        p.start()
    try:
        assert await queue_watcher._screen("SK-1") == queue_watcher.SKIP_ON_HOLD
    finally:
        patch.stopall()


@pytest.mark.asyncio
async def test_screen_checks_the_hold_before_spending_a_jira_fetch():
    """_screen is the free DB pass; a held ticket must not cost a round-trip."""
    for p in _patch_screen_db(has_plan=False, last_attempt=None, hold=object()):
        p.start()
    try:
        # Reached a verdict without the cooldown lookup mattering either way.
        assert await queue_watcher._screen("SK-1") is not None
    finally:
        patch.stopall()
