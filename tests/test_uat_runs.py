"""UAT runs: a ticket's live plan run by a headless runner, every case accounted for.

Why this file exists
--------------------
The runner used to read the plan from the tracker comment, check the build
itself with commands its allowlist refused, and mark passes through the
whole-set progress PUT. A dry run lost 43 of 49 cases to one refused build
check, and a result written while the backend was down simply did not exist.

Now the bot pins the run to the live plan, answers the build question per
repo, routes each case to the runner profile that can run it (or says why
none can), and takes results one at a time through an MCP server that spools
them first. These tests hold those promises:

* every case ends with a result or a reason code;
* a pass without evidence, or a Not Run without a reason, is refused;
* a result written during a backend outage is replayed, exactly once;
* an agent's pass is a proposal: it never ticks the tester's checklist.
"""

from __future__ import annotations

import json

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from src.app import main
from src.app.config import settings
from src.app.db import crud
from src.app.db import mongo as db_mongo
from src.app.db.models.plan import GeneratedPlan, PlanFormat
from src.app.db.models.run import RunStatus, RunType
from src.app.repositories import (
    plan_repository,
    run_repository,
    test_plan_progress_repository,
    uat_run_repository,
    user_repository,
)
from src.app.services import uat_runs
from src.app.services.uat_runs import RunRefused, route_cases
from src.uat_mcp import server as uat_mcp

MOBILE = "acme/mobile-app"
API = "acme/orders-api"

BODY = {
    "happy_path": [
        {"title": "Saves an order", "surface": "mobile", "steps": ["Open OrderScreen.swift flow"]},
        {"title": "Order endpoint returns 201", "surface": "backend_http",
         "steps": ["POST /orders (src/orders/handler.ts)"]},
    ],
    "edge_cases": [
        {"title": "Rejects an empty basket", "surface": "mobile", "steps": ["Tap Save"]},
        {"title": "Already unit-tested", "surface": "mobile", "covered_by_unit_test": True,
         "unit_test_ref": "OrderTests.swift"},
        {"title": "Feels fast on an old phone", "surface": "manual_only"},
    ],
    "integration_tests": [],
    "regression_checklist": ["Order history still loads (App/OrderScreen.swift)"],
}

TARGETS = [
    {"repo": MOBILE, "commit": "aaaaaaa1", "platform": "ios", "changed": ["App/OrderScreen.swift"], "deploy": []},
    {"repo": API, "commit": "bbbbbbb2", "platform": "backend", "changed": ["src/orders/handler.ts"], "deploy": []},
]

PROFILES = {
    "mobile": {"repos": [MOBILE], "surfaces": ["mobile"], "agent": "uat-runner",
               "cwd": "/tmp", "settings": "/tmp/s.json"},
    "api": {"repos": [API], "surfaces": ["backend_http"], "agent": "uat-runner",
            "cwd": "/tmp", "settings": "/tmp/s.json"},
}


def _build(repo, status, detail=None):
    return {"repo": repo, "status": status, "detail": detail, "merge_commit": "x"}


@pytest.fixture
def db(monkeypatch, tmp_path):
    client = AsyncMongoMockClient()
    database = client["testplan_test"]
    monkeypatch.setattr(db_mongo, "_client", client)
    monkeypatch.setattr(db_mongo, "_db", database)
    monkeypatch.setattr(db_mongo, "_supports_transactions", False)
    monkeypatch.setattr(settings, "qa_actor_email", "")
    monkeypatch.setattr(settings, "uat_evidence_root", str(tmp_path))
    return database


@pytest.fixture
def api(db):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


class FakeJira:
    def __init__(self, status="In Testing", fail=False):
        self.status = status
        self.fail = fail

    async def get_issue_status(self, key):
        if self.fail:
            raise httpx.ConnectError("vpn down")
        return self.status


async def _live_plan(db, body=BODY, ticket_key="SK-1", version=1):
    user = await user_repository.get_or_create_by_email(db, email="service@example.com")
    run = await run_repository.create(
        db, user_id=user.id, run_type=RunType.test_plan, ticket_keys=[ticket_key],
        model="claude-opus-5", llm_provider="claude", status=RunStatus.ok,
    )
    plan = await plan_repository.save_with_cases(
        db, run_id=run.id, format=PlanFormat.json, body=json.dumps(body), cases=[]
    )
    plan.version = version
    plan.jira_comment_id = f"c{plan.id}"
    from src.app.db.base import utcnow
    plan.posted_at = utcnow()
    await crud.save(db, plan)
    return plan


async def _start(db, monkeypatch, builds=None, profiles=PROFILES, **kw):
    async def fake_read(key, jira=None):
        return {"issue_type": "Story", "status": "In Testing", "dev_status_unavailable": False,
                "targets": TARGETS, "pr_count": 2,
                "builds": builds if builds is not None else
                [_build(MOBILE, "included"), _build(API, "unverified")]}
    monkeypatch.setattr(uat_runs, "read_ticket_builds", fake_read)
    return await uat_runs.start_run(db, ticket_key="SK-1", profiles=profiles, **kw)


def _pass(case_id, **extra):
    return {"case_id": case_id, "status": "passed",
            "evidence": [{"kind": "screenshot", "ref": "/e/shot.png", "text": "Order saved"}], **extra}


# --- Routing --------------------------------------------------------------------


def test_each_case_goes_to_the_profile_for_its_surface_and_repo():
    from src.app.services.progress_key import cases_by_id
    routing = route_cases(cases_by_id(BODY), TARGETS,
                          [_build(MOBILE, "included"), _build(API, "included")], PROFILES)
    assert routing["happy_path:0"]["leg"] == "mobile"
    assert routing["happy_path:1"]["leg"] == "api"
    assert routing["edge_cases:0"]["leg"] == "mobile"
    # A bare regression string goes to the one profile covering its repo.
    assert routing["regression_checklist:0"]["leg"] == "mobile"
    assert routing["edge_cases:1"]["not_run"]["code"] == "not_automatable"
    # Already unit-tested cases are outside the checklist a run is reviewed against.
    assert not any(k.startswith("covered_by_unit_test") for k in routing)


def test_a_repo_without_a_profile_is_named_and_the_rest_still_runs():
    from src.app.services.progress_key import cases_by_id
    routing = route_cases(cases_by_id(BODY), TARGETS,
                          [_build(MOBILE, "included"), _build(API, "included")],
                          {"mobile": PROFILES["mobile"]})
    assert routing["happy_path:1"]["leg"] is None
    assert routing["happy_path:1"]["not_run"]["code"] == "no_runner_profile"
    assert API in routing["happy_path:1"]["not_run"]["detail"]
    assert routing["happy_path:0"]["leg"] == "mobile"


def test_a_case_waits_only_for_the_builds_of_the_repos_it_exercises():
    from src.app.services.progress_key import cases_by_id
    routing = route_cases(cases_by_id(BODY), TARGETS,
                          [_build(MOBILE, "included"), _build(API, "not_included", "integ runs old")],
                          PROFILES)
    assert routing["happy_path:1"]["not_run"]["code"] == "waiting_for_build"
    assert routing["happy_path:0"]["leg"] == "mobile"


def test_an_unmerged_pr_and_an_unreadable_build_are_reasons_not_guesses():
    from src.app.services.progress_key import cases_by_id
    routing = route_cases(cases_by_id(BODY), TARGETS,
                          [_build(MOBILE, "unverifiable", "GitHub compare failed"),
                           _build(API, "pr_not_merged", "acme/orders-api #7 not merged")],
                          PROFILES)
    assert routing["happy_path:0"]["not_run"]["code"] == "blocked_environment"
    assert "unverifiable" in routing["happy_path:0"]["not_run"]["detail"]
    assert routing["happy_path:1"]["not_run"]["code"] == "pr_not_merged"


def test_a_spec_only_plan_routes_by_surface_only_when_one_profile_fits():
    from src.app.services.progress_key import cases_by_id
    two_mobile = {**PROFILES, "mobile2": {**PROFILES["mobile"], "repos": ["acme/other"]}}
    routing = route_cases(cases_by_id(BODY), [], [], two_mobile)
    assert routing["happy_path:0"]["not_run"]["code"] == "no_runner_profile"
    assert route_cases(cases_by_id(BODY), [], [], PROFILES)["happy_path:0"]["leg"] == "mobile"


# --- Builds ---------------------------------------------------------------------


def test_build_answer_uses_the_configured_environment(monkeypatch):
    target = {"repo": API, "commit": "bbbbbbb2", "deploy": [
        {"env": "integ", "included": True, "deployed_commit": "c1"},
        {"env": "staging", "included": False, "deployed_commit": "c0", "tag": "rel-c0"},
    ]}
    prs = [{"repository": API, "status": "MERGED", "number": 7}]
    monkeypatch.setattr(settings, "uat_test_env", "")
    assert uat_runs._build_for(target, prs)["status"] == "unverifiable"
    monkeypatch.setattr(settings, "uat_test_env", "integ")
    assert uat_runs._build_for(target, prs)["status"] == "included"
    monkeypatch.setattr(settings, "uat_test_env", "staging")
    assert uat_runs._build_for(target, prs)["status"] == "not_included"
    # Each repo uses the first of the names it has.
    monkeypatch.setattr(settings, "uat_test_env", "integ-west,staging")
    assert uat_runs._build_for(target, prs)["status"] == "not_included"
    monkeypatch.setattr(settings, "uat_test_env", "qa")
    assert "has: integ, staging" in uat_runs._build_for(target, prs)["detail"]


def test_no_source_is_unverified_and_an_open_pr_is_not_merged():
    target = {"repo": API, "commit": "b", "deploy": []}
    assert uat_runs._build_for(target, [{"repository": API, "status": "MERGED"}])["status"] == "unverified"
    out = uat_runs._build_for(target, [{"repository": API, "status": "OPEN", "number": 9}])
    assert out["status"] == "pr_not_merged" and "#9" in out["detail"]


def test_a_failed_comparison_is_unverifiable_never_included(monkeypatch):
    monkeypatch.setattr(settings, "uat_test_env", "integ")
    target = {"repo": API, "commit": "b", "deploy": [{"env": "integ", "included": None, "error": None}]}
    assert uat_runs._build_for(target, [{"repository": API, "status": "MERGED"}])["status"] == "unverifiable"


# --- Starting a run -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_pins_the_live_plan_and_records_reasons_for_unrunnable_cases(db, monkeypatch):
    plan = await _live_plan(db)
    run = await _start(db, monkeypatch, profiles={"mobile": PROFILES["mobile"]})
    assert run.plan_id == plan.id and run.status == "running"
    assert set(run.legs) == {"mobile"}
    assert run.legs["mobile"]["case_ids"] == ["happy_path:0", "edge_cases:0", "regression_checklist:0"]
    rows = await uat_run_repository.results_for_run(db, run.id)
    assert [(r.case_id, r.status, r.not_run["code"]) for r in rows] == [
        ("happy_path:1", "not_run", "no_runner_profile"),
        ("edge_cases:1", "not_run", "not_automatable"),
    ]
    assert run.evidence_dir.endswith(f"SK-1/run-{run.id}")


@pytest.mark.asyncio
async def test_no_posted_plan_is_refused_not_queued(db, monkeypatch):
    with pytest.raises(RunRefused) as e:
        await _start(db, monkeypatch)
    assert e.value.code == "waiting_for_plan_post"


@pytest.mark.asyncio
async def test_a_second_run_for_the_same_ticket_is_refused(db, monkeypatch):
    await _live_plan(db)
    first = await _start(db, monkeypatch)
    with pytest.raises(RunRefused) as e:
        await _start(db, monkeypatch)
    assert e.value.code == "run_active" and e.value.extra["run_id"] == first.id


@pytest.mark.asyncio
async def test_an_unreadable_ticket_refuses_the_run(db, monkeypatch):
    await _live_plan(db)

    async def broken(key, jira=None):
        raise httpx.ConnectError("vpn down")
    monkeypatch.setattr(uat_runs, "read_ticket_builds", broken)
    with pytest.raises(RunRefused) as e:
        await uat_runs.start_run(db, ticket_key="SK-1", profiles=PROFILES)
    assert e.value.code == "ticket_unreadable" and e.value.status == 503
    assert await uat_run_repository.active_for_ticket(db, ticket_key="SK-1") is None


@pytest.mark.asyncio
async def test_a_run_with_nothing_runnable_goes_straight_to_review(db, monkeypatch):
    await _live_plan(db)
    run = await _start(db, monkeypatch, profiles={})
    assert run.status == "awaiting_review"
    assert run.counts == {"passed": 0, "failed": 0, "not_run": 5}
    assert await uat_run_repository.active_for_ticket(db, ticket_key="SK-1") is None


# --- Recording results ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pass_without_evidence_is_refused_and_writes_nothing(db, monkeypatch, api):
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    url = f"/uat-runs/{run.id}/legs/mobile/results"
    r = await api.post(url, json={"case_id": "happy_path:0", "status": "passed"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "evidence_required"
    r = await api.post(url, json={"case_id": "happy_path:0", "status": "passed",
                                  "evidence": [{"kind": "screenshot"}]})
    assert r.status_code == 422, "an evidence item naming nothing is not evidence"
    assert await db["case_results"].count_documents({"source": "runner"}) == 0


@pytest.mark.asyncio
async def test_a_not_run_needs_a_known_code_and_a_detail(db, monkeypatch, api):
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    url = f"/uat-runs/{run.id}/legs/mobile/results"
    for nr in (None, {"code": "because"}, {"code": "not_automatable", "detail": "  "}):
        r = await api.post(url, json={"case_id": "happy_path:0", "status": "not_run", "not_run": nr})
        assert r.status_code == 422 and r.json()["detail"]["error"] == "reason_required"
    r = await api.post(url, json={"case_id": "happy_path:0", "status": "not_run",
                                  "not_run": {"code": "not_automatable", "detail": "needs a second device"}})
    assert r.status_code == 200 and r.json()["recorded"] is True


@pytest.mark.asyncio
async def test_a_leg_cannot_record_another_legs_case_or_an_unknown_one(db, monkeypatch, api):
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    r = await api.post(f"/uat-runs/{run.id}/legs/mobile/results", json=_pass("happy_path:1"))
    assert r.status_code == 422 and r.json()["detail"]["error"] == "wrong_leg"
    r = await api.post(f"/uat-runs/{run.id}/legs/mobile/results", json=_pass("happy_path:9"))
    assert r.status_code == 422 and r.json()["detail"]["error"] == "unknown_case_id"


@pytest.mark.asyncio
async def test_an_agent_pass_is_named_and_does_not_tick_the_checklist(db, monkeypatch, api):
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    r = await api.post(f"/uat-runs/{run.id}/legs/mobile/results", json=_pass("happy_path:0"))
    assert r.json()["recorded"] is True
    row = await db["case_results"].find_one({"source": "runner"})
    assert row["actor"] == {"kind": "agent", "name": "uat-runner", "leg": "mobile", "run_id": run.id}
    assert row["review_approved"] is False and row["progress_key"] == run.progress_key
    view = await test_plan_progress_repository.get_progress(db, progress_key=run.progress_key)
    assert json.loads(view.checked_ids) == []


@pytest.mark.asyncio
async def test_resending_the_same_client_id_writes_one_row(db, monkeypatch, api):
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    for _ in range(2):
        r = await api.post(f"/uat-runs/{run.id}/legs/mobile/results",
                           json=_pass("happy_path:0", client_id="abc-1"))
    assert r.json()["duplicate"] is True
    assert await db["case_results"].count_documents({"client_id": "abc-1"}) == 1


# --- Continue / cancel ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_replaced_plan_stops_the_run(db, monkeypatch):
    plan = await _live_plan(db)
    run = await _start(db, monkeypatch)
    # A regeneration is posted: the old plan stops being live.
    await db["generated_plans"].update_one({"_id": plan.id}, {"$set": {"jira_comment_id": None}})
    newer = await _live_plan(db, version=2)
    out = await uat_runs.should_continue(db, run, jira=FakeJira())
    assert out["continue"] is False and f"plan {newer.id} v2" in out["reason"]
    run = await uat_run_repository.get(db, run.id)
    assert run.status == "cancelled" and run.status_detail == "plan_changed"


@pytest.mark.asyncio
async def test_a_ticket_moved_out_of_its_status_stops_the_run(db, monkeypatch):
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    out = await uat_runs.should_continue(db, run, jira=FakeJira(status="In Development"))
    assert out == {"continue": False, "reason": "ticket moved to In Development",
                   "checks": {"plan_live": True, "ticket_status": "In Development"}}


@pytest.mark.asyncio
async def test_a_check_that_could_not_be_made_says_so(db, monkeypatch):
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    out = await uat_runs.should_continue(db, run, jira=FakeJira(fail=True))
    assert out["continue"] is True
    assert out["checks"]["ticket_status"] is None and "ConnectError" in out["checks"]["ticket_status_error"]


# --- Finishing ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_case_ends_with_a_result_or_a_reason(db, monkeypatch, api):
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    await api.post(f"/uat-runs/{run.id}/legs/mobile/results", json=_pass("happy_path:0"))
    r = await api.post(f"/uat-runs/{run.id}/legs/mobile/finalize",
                       json={"exit_code": 0, "cost_usd": 1.25, "permission_denials": [{"tool_name": "Bash"}]})
    view = r.json()
    assert view["status"] == "running", "the api leg has not run yet"
    by_id = {c["id"]: c["result"] for c in view["cases"]}
    assert by_id["edge_cases:0"]["not_run"]["code"] == "run_aborted"
    assert "without recording" in by_id["edge_cases:0"]["not_run"]["detail"]

    r = await api.post(f"/uat-runs/{run.id}/legs/api/finalize",
                       json={"exit_code": 1, "detail": "simulator crashed", "cost_usd": 0.5})
    view = r.json()
    assert view["status"] == "awaiting_review"
    assert "api (exit 1)" in view["status_reason"]
    assert view["counts"] == {"passed": 1, "failed": 0, "not_run": 4}
    assert all(c["result"] for c in view["cases"])
    run = await uat_run_repository.get(db, run.id)
    assert run.cost_usd == 1.75 and run.active_ticket is None and run.finished_at


@pytest.mark.asyncio
async def test_a_wall_clock_stop_marks_unreached_cases_dropped(db, monkeypatch, api):
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    r = await api.post(f"/uat-runs/{run.id}/legs/mobile/finalize",
                       json={"exit_code": -2, "missing_code": "dropped_time_cap", "detail": "cap 60 min"})
    codes = {c["id"]: (c["result"] or {}).get("not_run", {}).get("code") for c in r.json()["cases"]}
    assert codes["happy_path:0"] == "dropped_time_cap"


@pytest.mark.asyncio
async def test_a_cancelled_run_fills_its_cases_as_cancelled(db, monkeypatch, api):
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    await api.post(f"/uat-runs/{run.id}/cancel", json={"reason": "ticket moved to In Development"})
    r = await api.post(f"/uat-runs/{run.id}/legs/mobile/finalize", json={"exit_code": 0})
    case = next(c for c in r.json()["cases"] if c["id"] == "happy_path:0")
    assert case["result"]["not_run"] == {"code": "run_cancelled", "detail": "ticket moved to In Development"}
    assert r.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_finalizing_a_leg_twice_changes_nothing(db, monkeypatch, api):
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    for _ in range(2):
        await api.post(f"/uat-runs/{run.id}/legs/mobile/finalize", json={"exit_code": 0})
    assert await db["case_results"].count_documents({"source": "finalize"}) == 3


# --- The runner's MCP server: spool and replay -----------------------------------


@pytest.fixture
def mcp_env(db, monkeypatch, tmp_path):
    """Point the MCP server's HTTP calls at the app in-process."""
    real = httpx.AsyncClient

    def client(*a, **kw):
        kw.pop("follow_redirects", None)
        # As uvicorn does: an unhandled error is a 500 response, not an exception.
        return real(transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False),
                    base_url="http://test")
    monkeypatch.setattr(uat_mcp.httpx, "AsyncClient", client)
    return tmp_path


async def _mcp_env(run, tmp_path):
    return {"api": "http://test", "run_id": str(run.id), "leg": "mobile",
            "evidence": str(tmp_path / "ev"), "hosts": {"api.example.test"}}


@pytest.mark.asyncio
async def test_a_result_written_during_a_db_outage_is_replayed_exactly_once(db, monkeypatch, mcp_env):
    """The P2 promise: a forced DB outage mid-run loses nothing."""
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    env = await _mcp_env(run, mcp_env)

    ok = await uat_mcp._record(env, _pass("happy_path:0"))
    assert ok["result"] == "recorded"

    from pymongo.errors import ServerSelectionTimeoutError
    real_insert = crud.insert

    async def outage(*a, **kw):
        raise ServerSelectionTimeoutError("no primary")
    monkeypatch.setattr(crud, "insert", outage)
    out = await uat_mcp._record(env, _pass("edge_cases:0"))
    assert out["result"] == "spooled" and "500" in out["why"]
    assert [e["payload"]["case_id"] for e in uat_mcp.pending(env["evidence"])] == ["edge_cases:0"]

    monkeypatch.setattr(crud, "insert", real_insert)
    finished = await uat_mcp._finish(env)
    assert finished["replayed"]["sent"] == 1 and finished["missing"] == ["regression_checklist:0"]
    assert uat_mcp.pending(env["evidence"]) == []
    # Replaying again sends nothing and writes nothing.
    assert (await uat_mcp.replay(env))["sent"] == 0
    rows = await db["case_results"].count_documents({"source": "runner"})
    assert rows == 2


@pytest.mark.asyncio
async def test_a_refused_result_is_not_replayed(db, monkeypatch, mcp_env):
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    env = await _mcp_env(run, mcp_env)
    out = await uat_mcp._record(env, {"case_id": "happy_path:0", "status": "passed"})
    assert out["result"] == "refused" and out["detail"]["error"] == "evidence_required"
    assert uat_mcp.pending(env["evidence"]) == []


@pytest.mark.asyncio
async def test_finish_run_lists_cases_still_owed(db, monkeypatch, mcp_env):
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    env = await _mcp_env(run, mcp_env)
    out = await uat_mcp._finish(env)
    assert out["missing"] == ["happy_path:0", "edge_cases:0", "regression_checklist:0"]


@pytest.mark.asyncio
async def test_http_request_refuses_hosts_off_the_allowlist(db, monkeypatch, mcp_env):
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    env = await _mcp_env(run, mcp_env)
    out = await uat_mcp._http(env, {"method": "DELETE", "url": "https://prod.example.test/orders/1"})
    assert out["error"] == "host_not_allowed"
    out = await uat_mcp._http(env, {"method": "GET", "url": "file:///etc/passwd"})
    assert out["error"] == "bad_url"


def test_http_evidence_redacts_secrets():
    assert uat_mcp._redact({"Authorization": "Bearer x", "Accept": "json"}) == {
        "Authorization": "<redacted>", "Accept": "json"}


@pytest.mark.asyncio
async def test_the_run_index_is_small_and_each_case_is_read_on_its_own(db, monkeypatch, mcp_env):
    """A 49-case leg's full view overflowed one tool result on a real run."""
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    env = await _mcp_env(run, mcp_env)
    index = uat_mcp.compact(await uat_mcp._get_run(env), hosts=env["hosts"])
    assert index["http_allowed_hosts"] == ["api.example.test"]
    assert [c["id"] for c in index["cases"]] == ["happy_path:0", "edge_cases:0", "regression_checklist:0"]
    assert "case" not in index["cases"][0] and index["cases"][0]["builds"] == {MOBILE: "included"}
    full = await uat_mcp._get_case(env, "happy_path:0")
    assert full["case"]["steps"] == ["Open OrderScreen.swift flow"]
    assert (await uat_mcp._get_case(env, "happy_path:1"))["error"] == "unknown_case_id"
