"""Unattended UAT runs: queued by the sweep, claimed and leased by the dispatcher.

P4 of docs/design/uat-loop.md: a board move ends in a run awaiting review with
nothing typed. These tests hold the dispatcher's promises:

* one run per plan version, queued as a DB write only;
* one run holds the device at a time, by lease;
* a dead dispatcher's run is recovered, and its unfinished cases retried once;
* a ticket that moved, or a plan that changed, cancels the run before it runs;
* cases waiting for a deploy are rechecked and run when it lands, without
  rerunning what already has a result;
* no run starts that cannot finish before the working window closes;
* the health endpoint says when the dispatcher stopped.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.app import watch_schedule
from src.app.db.base import utcnow
from src.app.repositories import uat_run_repository
from src.app.services import queue_watcher, uat_dispatch, uat_runs
from tests.test_uat_runs import (  # noqa: F401
    API, MOBILE, PROFILES, FakeJira, _build, _live_plan, _pass, api, db,
)

HOLDER = {"holder": "test", "host": "h", "pid": 1}


def _builds(monkeypatch, builds):
    from tests.test_uat_runs import TARGETS

    async def fake_read(key, jira=None):
        return {"issue_type": "Story", "status": "In Testing", "dev_status_unavailable": False,
                "targets": TARGETS, "pr_count": 2, "builds": builds}
    monkeypatch.setattr(uat_runs, "read_ticket_builds", fake_read)


ALL_IN = [_build(MOBILE, "included"), _build(API, "included")]


@pytest.fixture(autouse=True)
def always_open(monkeypatch):
    monkeypatch.setattr(watch_schedule, "minutes_left", lambda now=None: None)


async def _queued(db, **kw):
    await _live_plan(db)
    return await uat_runs.queue_run(db, ticket_key="SK-1", ticket_status="In Testing", **kw)


async def _replace_plan(db):
    """Post a v2: as in Jira, the old plan's comment is the one now updated."""
    from src.app.db.models.plan import GeneratedPlan
    await db[GeneratedPlan.__collection__].update_many({}, {"$set": {"jira_comment_id": None}})
    return await _live_plan(db, version=2)


async def _runner_passes_all(db, run_id):
    """The runner records a pass for every case its legs owe, then closes."""
    run = await uat_run_repository.get(db, run_id)
    latest = uat_runs.latest_by_case(await uat_run_repository.results_for_run(db, run_id))
    for name, leg in run.legs.items():
        if leg.get("status") == "pending":
            for cid in uat_runs.owed_cases(run, name, latest):
                await uat_runs.record_result(db, run, leg=name, payload=_pass(cid))
    return await _finish_legs(db, run_id)


async def _claim(db, jira=None):
    return await uat_dispatch.claim(db, holder=HOLDER, jira=jira or FakeJira(), profiles=PROFILES)


async def _finish_legs(db, run_id, info=None):
    run = await uat_run_repository.get(db, run_id)
    for name, leg in run.legs.items():
        if leg.get("status") == "pending":
            run = await uat_runs.finalize_leg(db, run, name, info or {"exit_code": 0})
    return await uat_run_repository.get(db, run_id)


@pytest.mark.asyncio
async def test_queue_is_a_write_and_one_run_per_plan(db):
    run = await _queued(db)
    assert run.status == "queued" and run.routing == {} and run.legs == {}
    assert run.active_ticket == "SK-1"
    assert await uat_runs.queue_run(db, ticket_key="SK-1") is None
    run.status = "approved"
    await uat_run_repository.save(db, run)
    assert await uat_runs.queue_run(db, ticket_key="SK-1") is None, "a reviewed plan is not rerun"


@pytest.mark.asyncio
async def test_a_new_plan_supersedes_a_run_still_awaiting_review(db):
    old = await _queued(db)
    old.status = "awaiting_review"
    await uat_run_repository.save(db, old)
    await _replace_plan(db)
    new = await uat_runs.queue_run(db, ticket_key="SK-1")
    old = await uat_run_repository.get(db, old.id)
    assert new is not None and old.status == "superseded" and f"run {new.id}" in old.status_reason


@pytest.mark.asyncio
async def test_claim_routes_and_leases_and_holds_the_device(db, monkeypatch):
    _builds(monkeypatch, ALL_IN)
    run = await _queued(db)
    out = await _claim(db)
    assert out["claimed"] == run.id
    run = await uat_run_repository.get(db, run.id)
    assert run.status == "running" and run.rounds == 1 and run.lease["holder"] == "test"
    assert {n for n, l in run.legs.items() if l["status"] == "pending"} == {"mobile", "api"}

    await _live_plan(db, ticket_key="SK-2")
    await uat_runs.queue_run(db, ticket_key="SK-2")
    out = await _claim(db)
    assert out["claimed"] is None and "holds the device" in out["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [("In Development", "ticket moved to In Development")])
async def test_a_moved_ticket_cancels_before_anything_runs(db, monkeypatch, status, expected):
    _builds(monkeypatch, ALL_IN)
    run = await _queued(db)
    out = await _claim(db, jira=FakeJira(status=status))
    run = await uat_run_repository.get(db, run.id)
    assert out["claimed"] is None and run.status == "cancelled" and run.status_reason == expected
    assert out["events"][0]["event"] == "cancelled" and run.active_ticket is None


@pytest.mark.asyncio
async def test_a_replaced_plan_cancels_the_queued_run(db, monkeypatch):
    _builds(monkeypatch, ALL_IN)
    run = await _queued(db)
    await _replace_plan(db)
    await _claim(db)
    run = await uat_run_repository.get(db, run.id)
    assert run.status == "cancelled" and "stale_plan" in run.status_reason


@pytest.mark.asyncio
async def test_an_unreadable_tracker_retries_later_and_says_so(db, monkeypatch):
    _builds(monkeypatch, ALL_IN)
    run = await _queued(db)
    out = await _claim(db, jira=FakeJira(fail=True))
    run = await uat_run_repository.get(db, run.id)
    assert out["claimed"] is None and run.status == "queued" and run.lease is None
    assert run.not_before > utcnow() and "tracker unreadable" in out["events"][0]["detail"]


@pytest.mark.asyncio
async def test_a_dead_dispatcher_is_recovered_and_its_cases_retried_once(db, monkeypatch, api):
    _builds(monkeypatch, ALL_IN)
    run = await _queued(db)
    await _claim(db)
    async with api as c:
        await c.post(f"/uat-runs/{run.id}/legs/mobile/results", json=_pass("happy_path:0"))
    run = await uat_run_repository.get(db, run.id)
    run.lease["expires_at"] = utcnow() - timedelta(minutes=1)
    await uat_run_repository.save(db, run)

    out = await _claim(db)
    assert out["events"][0]["event"] == "lease_expired"
    assert out["claimed"] == run.id, "requeued and taken again in the same tick"
    run = await uat_run_repository.get(db, run.id)
    assert run.rounds == 2
    view = await uat_runs.run_view(db, run, leg="mobile")
    assert "happy_path:0" not in view["missing"] and view["missing"]

    run = await _finish_legs(db, run.id, {"exit_code": -1, "detail": "crashed again"})
    assert run.status == "awaiting_review", "one retry, then the review opens"


@pytest.mark.asyncio
async def test_waiting_cases_run_when_the_deploy_lands(db, monkeypatch):
    _builds(monkeypatch, [_build(MOBILE, "included"), _build(API, "not_included", "not on integ")])
    run = await _queued(db)
    assert (await _claim(db))["claimed"] == run.id
    run = await uat_run_repository.get(db, run.id)
    assert set(run.legs) == {"mobile"}, "the api cases wait"
    run = await _runner_passes_all(db, run.id)
    assert run.status == "waiting_for_build" and run.not_before > utcnow()
    assert "wait for a deploy" in run.status_reason
    assert (await _claim(db))["claimed"] is None, "not due before its recheck"

    run.not_before = utcnow() - timedelta(seconds=1)
    await uat_run_repository.save(db, run)
    out = await _claim(db)
    assert out["claimed"] is None and out["events"][0]["event"] == "still_waiting"

    waiting = {c["id"] for c in (await uat_runs.run_view(db, run))["cases"]
               if ((c["result"] or {}).get("not_run") or {}).get("code") == "waiting_for_build"}
    _builds(monkeypatch, ALL_IN)
    run = await uat_run_repository.get(db, run.id)
    run.not_before = utcnow() - timedelta(seconds=1)
    await uat_run_repository.save(db, run)
    assert (await _claim(db))["claimed"] == run.id
    run = await uat_run_repository.get(db, run.id)
    assert run.legs["api"]["status"] == "pending"
    owed = set()
    for name in run.legs:
        if run.legs[name]["status"] == "pending":
            owed |= set((await uat_runs.run_view(db, run, leg=name))["missing"])
    # Exactly the cases that waited run now, on whichever leg drives them (a
    # mobile case calling the new API waited for both builds).
    assert owed == waiting
    passed_before = (await uat_runs.run_view(db, run))["counts"]["passed"]
    run = await _runner_passes_all(db, run.id)
    assert run.status == "awaiting_review"
    assert run.counts["passed"] == passed_before + len(owed)


@pytest.mark.asyncio
async def test_waiting_gives_up_after_the_limit(db, monkeypatch):
    _builds(monkeypatch, [_build(MOBILE, "included"), _build(API, "not_included", "not on integ")])
    run = await _queued(db)
    await _claim(db)
    run = await _runner_passes_all(db, run.id)
    run.waiting_since = utcnow() - timedelta(hours=uat_runs.settings.uat_build_wait_hours + 1)
    run.not_before = utcnow() - timedelta(seconds=1)
    await uat_run_repository.save(db, run)
    await _claim(db)
    run = await uat_run_repository.get(db, run.id)
    assert run.status == "awaiting_review" and "still waiting for a deploy" in run.status_reason


@pytest.mark.asyncio
async def test_no_run_starts_that_cannot_finish_in_the_window(db, monkeypatch):
    _builds(monkeypatch, ALL_IN)
    await _queued(db)
    monkeypatch.setattr(watch_schedule, "minutes_left", lambda now=None: 20.0)
    out = await _claim(db)
    assert out["claimed"] is None and "closes in 20 min" in out["reason"]


@pytest.mark.asyncio
async def test_alerts_fire_once(db):
    run = await _queued(db)
    assert await uat_dispatch.alert_once(db, run.id, "awaiting_review")
    assert not await uat_dispatch.alert_once(db, run.id, "awaiting_review")


@pytest.mark.asyncio
async def test_health_says_when_the_dispatcher_stopped(db, monkeypatch, api):
    async with api as c:
        assert (await c.get("/health/uat")).json()["state"] == "never"
        await c.post("/uat-dispatch/heartbeat", json={"outcome": "idle"})
        h = (await c.get("/health/uat")).json()
        assert h["state"] == "ok"
    await db[uat_dispatch.HEARTBEAT_COLLECTION].update_one(
        {"_id": uat_dispatch.HEARTBEAT_ID}, {"$set": {"at": utcnow() - timedelta(hours=2)}})
    monkeypatch.setattr(watch_schedule, "window_start", lambda now=None: None)
    h = await uat_dispatch.health(db)
    assert h["state"] == "late" and h["minutes_since"] >= 119


@pytest.mark.asyncio
async def test_the_sweep_queues_only_when_enabled(db, monkeypatch):
    await _live_plan(db)
    result = queue_watcher.AutoPostSweepResult()
    await queue_watcher._queue_uat_run("SK-1", "In Testing", result)
    assert result.uat_queued == ["SK-1"]
    await queue_watcher._queue_uat_run("SK-1", "In Testing", result)
    assert result.uat_queued == ["SK-1"] and result.uat_errors == {}
    await queue_watcher._queue_uat_run("SK-9", "In Testing", result)
    assert "SK-9" not in result.uat_errors, "no plan posted yet is not an error"
