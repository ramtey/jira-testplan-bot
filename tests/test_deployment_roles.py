"""Build checks and log steps target the deployment that serves the cases (rule AJ).

Why this file exists
--------------------
SK-3060 (plan 721 v1): the app runs as two deployments in two deployments
repos. One is an internal Agenda worker (no host, ``RUN_AGENDA_CRONS: "true"``)
and the other is the public copy whose Envoy host the tRPC routes are served
on. The plan's build check and its boot-log step both named the worker,
picked by environment name. Loki for the worker showed only health checks, so
a pass there proved nothing about the routes under test. Both copies happened
to run the same commit, so the plan's conclusion was right only by luck.

The fixture in tests/fixtures/multi_deployment is that layout with every
hostname, account id, org and repo replaced by a placeholder.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.app import build_targets as bt
from src.app import deployment_roles as dr
from src.app import evidence_checks as ev
from src.app.deploy_state import read_deploy_state, render_deploy_state

FIX = Path(__file__).parent / "fixtures" / "multi_deployment"
WORKER = (FIX / "worker_values.yaml").read_text()
PUBLIC = (FIX / "public_values.yaml").read_text()
PLAN = json.loads((FIX / "plan_v1.json").read_text())

APP = "example-org/calc-app"
WORKER_SRC = "example-org/ops-deployments/team/calc-app/staging-east/values.yaml"
PUBLIC_SRC = "example-org/public-deployments/team/calc-app/staging.staging-west/values.yaml"
HOST = "calc-app-staging.example.com"
MERGE = "c66d2f7aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PR = {"repository": APP, "number": 412, "url": "u412", "status": "MERGED", "head_sha": "tip",
      "merge_commit_sha": MERGE, "merged_at": "2026-11-17T10:00:00Z",
      "files_changed": [{"filename": "apps/server/src/routers/gfeDebug.ts"},
                        {"filename": "apps/server/src/routers/index.ts"},
                        {"filename": "apps/server/src/jobs/rollup.ts"}]}


class FakeGitHub:
    def __init__(self, files, trees=None):
        self.files, self.trees = files, trees or {}

    async def fetch_files_at_ref(self, repo, ref, paths):
        return {p: self.files[f"{repo}/{p}"] for p in paths if f"{repo}/{p}" in self.files}

    async def compare_commits(self, repo, base, head):
        return {"status": "identical", "behind_by": 0}

    async def list_tree(self, repo, ref):
        return self.trees.get(repo)


def _plan():
    p = copy.deepcopy(PLAN)
    return SimpleNamespace(happy_path=p["happy_path"], edge_cases=p["edge_cases"],
                           integration_tests=p["integration_tests"], security_negative_tests=[],
                           regression_checklist=p["regression_checklist"])


async def _deploy_state(sources):
    files = {WORKER_SRC: WORKER, PUBLIC_SRC: PUBLIC}
    return await read_deploy_state(FakeGitHub(files), [PR], {APP: sources})


async def _run(sources):
    """The plan's build-check, log and re-check stages, as run_evidence_checks runs them."""
    state = await _deploy_state(sources)
    targets = bt.build_targets([PR], [{"repo": APP, "platform": "backend"}], state)
    plan, top = _plan(), {}
    ev.enforce_deploy_precondition(plan, targets=targets, when="2026-11-18 21:00 UTC", top=top)
    logs = dr.check_log_steps(plan, targets)
    verified = dr.verify_build_check(plan, top, targets)
    return plan, top, logs, verified


# ---------------------------------------------------------------------------
# 1. Classification
# ---------------------------------------------------------------------------

def test_worker_is_internal_and_its_base_url_is_not_a_host():
    c = dr.classify_values(WORKER)
    assert c["role"] == "internal"
    assert c["hosts"] == []
    assert any('RUN_AGENDA_CRONS: "true"' in s for s in c["signals"])


def test_public_copy_serves_its_envoy_host_and_env_hosts_do_not_count():
    c = dr.classify_values(PUBLIC)
    assert c["role"] == "serving"
    assert c["hosts"] == [HOST]          # not FILES_DB_HOST, not EXPO_PUBLIC_BASE_URL
    assert any(s.startswith("line ") and HOST in s for s in c["signals"])


@pytest.mark.parametrize("text,role,hosts", [
    ("ingress:\n  enabled: true\n  hosts:\n    - api.example.com\n", "serving", ["api.example.com"]),
    ("ingress:\n  hosts: [a.example.com, b.example.com]\n", "serving", ["a.example.com", "b.example.com"]),
    ("ingress:\n  enabled: false\nconfig:\n  PORT: 80\n", "internal", []),
    ("config:\n  QUEUE_WORKER: true\n", "internal", []),
    ("kind: CronJob\nschedule: \"*/5 * * * *\"\n", "internal", []),
    ("image:\n  tag: main-abc1234\n", "unknown", []),
    # A worker *setting* is not a worker flag, and a comment about some
    # other "Worker" (a Cloudflare Worker) is not this copy calling itself one.
    ("config:\n  services__worker__maximumCommandsPerThrottlePartition: \"1\"\n", "unknown", []),
    ("# (npe|prod), set by the Worker, separates them\nimage:\n  tag: x-abc1234\n", "unknown", []),
    ("config:\n  RUN_WORKER: \"true\"\n", "internal", []),
    # Serving copy that also runs crons still serves the cases.
    ("config:\n  RUN_AGENDA_CRONS: \"true\"\nenvoy-host:\n  hosts:\n    - host: x.example.com\n",
     "serving", ["x.example.com"]),
])
def test_classify_values_layouts(text, role, hosts):
    c = dr.classify_values(text)
    assert (c["role"], c["hosts"]) == (role, hosts)


@pytest.mark.asyncio
async def test_deploy_state_carries_roles_and_the_prompt_says_which_serves():
    state = await _deploy_state({"staging-east": WORKER_SRC, "staging.staging-west": PUBLIC_SRC})
    roles = {e["env"]: (e["role"], e["hosts"]) for e in state}
    assert roles == {"staging-east": ("internal", []), "staging.staging-west": ("serving", [HOST])}
    text = render_deploy_state(state)
    assert f"SERVING: routes {HOST}" in text
    assert "INTERNAL worker/cron copy, no host" in text


@pytest.mark.asyncio
async def test_discovery_keeps_both_copies_when_two_deployments_repos_share_an_env_name():
    trees = {"example-org/ops-deployments": ["team/calc-app/staging/values.yaml"],
             "example-org/public-deployments": ["team/calc-app/staging/values.yaml"]}
    found = await bt.discover_deployments(
        FakeGitHub({}, trees), [PR],
        deployments_repos=["example-org/ops-deployments", "example-org/public-deployments"])
    srcs = found["deploy_sources"][APP]
    assert sorted(srcs.values()) == ["example-org/ops-deployments/team/calc-app/staging/values.yaml",
                                     "example-org/public-deployments/team/calc-app/staging/values.yaml"]


# ---------------------------------------------------------------------------
# 2–3. The build check and the log steps name the public copy for API cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_check_reads_the_public_deployment_and_lists_the_worker_apart():
    _, top, _, verified = await _run({"staging-east": WORKER_SRC, "staging.staging-west": PUBLIC_SRC})
    step = top["build_check"]["step"]
    primary, _, rest = step.partition(" Internal copies (no host")
    if not rest:
        primary, _, rest = step.partition(" Not this check")
    assert f"read the image tag in {PUBLIC_SRC}" in primary
    assert WORKER_SRC not in primary and "staging-east" not in primary
    # The worker is still named, apart, with why — and since one case tests a
    # cron job, it is checked for that case only.
    assert WORKER_SRC in rest and 'RUN_AGENDA_CRONS: "true"' in rest
    assert "only for the cases that test cron" in rest
    assert verified["downgraded"] == [] or all(d["where"] != "build_check" for d in verified["downgraded"])
    assert any(HOST in c for c in verified["confirmed"])


@pytest.mark.asyncio
async def test_without_a_cron_case_the_worker_is_named_only_as_not_this_check():
    state = await _deploy_state({"staging-east": WORKER_SRC, "staging.staging-west": PUBLIC_SRC})
    targets = bt.build_targets([PR], [{"repo": APP, "platform": "backend"}], state)
    plan, top = _plan(), {}
    plan.edge_cases = []
    ev.enforce_deploy_precondition(plan, targets=targets, when="now", top=top)
    step = top["build_check"]["step"]
    assert "Not this check: staging-east" in step
    assert "only for the cases that test cron" not in step


@pytest.mark.asyncio
async def test_boot_log_step_on_the_worker_is_marked_with_the_public_copy():
    plan, _, logs, _ = await _run({"staging-east": WORKER_SRC, "staging.staging-west": PUBLIC_SRC})
    step = plan.happy_path[1]["steps"][0]
    assert step.startswith("Wrong deployment for these logs (rule AJ):")
    assert f"served by staging.staging-west ({PUBLIC_SRC})" in step
    assert step.endswith("Open the staging-east calc-app server pod logs in Grafana (Loki)")
    assert len(plan.happy_path[1]["steps"]) == 3          # step numbers hold
    assert any(l["action"] == "marked" and l["step"] == 1 for l in logs)
    # A cron case reading the worker's logs is right, and is pointed at it.
    cron = plan.edge_cases[0]["steps"][1]
    assert "Wrong deployment" not in cron
    assert "cron / job behaviour runs on staging-east" in cron


@pytest.mark.asyncio
async def test_model_written_build_line_on_the_worker_is_downgraded():
    plan, _, _, verified = await _run({"staging-east": WORKER_SRC, "staging.staging-west": PUBLIC_SRC})
    pre = plan.happy_path[0]["preconditions"]
    assert "Build check names the wrong deployment (rule AJ)" in pre
    assert any(d["where"].startswith("happy_path:0") for d in verified["downgraded"])


@pytest.mark.asyncio
async def test_rechecks_are_idempotent():
    plan, top, _, _ = await _run({"staging-east": WORKER_SRC, "staging.staging-west": PUBLIC_SRC})
    state = await _deploy_state({"staging-east": WORKER_SRC, "staging.staging-west": PUBLIC_SRC})
    targets = bt.build_targets([PR], [{"repo": APP, "platform": "backend"}], state)
    before = copy.deepcopy((plan.happy_path, plan.edge_cases, top))
    dr.check_log_steps(plan, targets)
    dr.verify_build_check(plan, top, targets)
    assert (plan.happy_path, plan.edge_cases, top) == before


# ---------------------------------------------------------------------------
# 4. Only the worker was found (the public repo was not read): ask, never pick
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_only_the_worker_found_asks_which_deployment_serves_the_host():
    plan, top, _, verified = await _run({"staging-east": WORKER_SRC})
    step = top["build_check"]["step"]
    assert f"confirm which deployment serves {HOST}".lower() in step.lower()
    assert f"read the image tag in {WORKER_SRC}" not in step
    log = plan.happy_path[1]["steps"][0]
    assert log.startswith("Confirm the deployment first (rule AJ):")
    assert f"confirm which deployment serves {HOST}" in log


@pytest.mark.asyncio
async def test_unclassified_candidates_are_all_listed_with_a_confirm():
    plain = "app:\n  image:\n    tag: main-c66d2f7\n"
    files = {WORKER_SRC: plain, PUBLIC_SRC: plain}
    state = await read_deploy_state(FakeGitHub(files), [PR],
                                    {APP: {"staging-east": WORKER_SRC, "staging.staging-west": PUBLIC_SRC}})
    targets = bt.build_targets([PR], [{"repo": APP, "platform": "backend"}], state)
    clause = bt.check_clause(targets[0], "now", {"hosts": [HOST], "jobs": False})
    assert WORKER_SRC in clause and PUBLIC_SRC in clause
    assert f"Confirm which deployment serves {HOST}" in clause


@pytest.mark.asyncio
async def test_environments_of_one_service_in_one_repo_get_no_question():
    """integ / staging / prod of one service with no host in their values
    files are environments, not competing copies: the check is unchanged."""
    plain = "app:\n  image:\n    tag: main-c66d2f7\n"
    a, b = "example-org/ops-deployments/team/calc-app/integ/values.yaml", WORKER_SRC
    state = await read_deploy_state(FakeGitHub({a: plain, b: plain}), [PR],
                                    {APP: {"integ": a, "staging-east": b}})
    targets = bt.build_targets([PR], [{"repo": APP, "platform": "backend"}], state)
    clause = bt.check_clause(targets[0], "now", {"hosts": [HOST], "jobs": False})
    assert f"read the image tag in {a} or {b}" in clause
    assert "Confirm which" not in clause and "rule AJ" not in clause
    plan = SimpleNamespace(happy_path=[{"title": "t", "steps": ["Open the staging-east server pod logs"],
                                        "environment": "staging"}],
                           edge_cases=[], integration_tests=[], security_negative_tests=[],
                           regression_checklist=[])
    assert dr.check_log_steps(plan, targets) == []


def test_hosts_a_case_calls_leave_out_tools_and_file_names():
    text = ("Call https://calc-app-staging.example.com/api/trpc/x, then open https://github.com/o/r/pull/1, "
            "grafana.example.com and values.yaml; see https://acme.atlassian.net/browse/SK-1")
    assert dr.hosts_in(text) == [HOST]
    # Object paths in step text are not hosts.
    assert dr.hosts_in("check profile.result.id and error.response.status in req.body.templateIds") == []
    assert dr.hosts_in("the ASP.NET handler") == []
    assert dr.app_hosts(["us.posthog.com", HOST], "example-org/calc-app") == [HOST]


def test_tiers():
    assert [dr.tier(e) for e in ("staging-east", "staging.staging-west", "ss-prod-west",
                                 "prod.fnf-prod-west", "integ-west", "dev.integ-west", "offer-management")] == \
        ["staging", "staging", "prod", "prod", "integ", "integ", None]


def test_a_directory_named_for_the_service_is_not_an_environment_mention():
    d = {"env": "offer-management", "source": "o/deps/om/offer-management/values.yaml"}
    assert not dr._env_named("Sign in to offer-management on integ", d)
    assert dr._env_named("Open the staging-east pods", {"env": "staging-east", "source": "x"})
    assert dr._env_named("Open the staging-west pods", {"env": "staging.staging-west", "source": "x"})


def test_entries_without_roles_keep_the_old_check():
    t = {"repo": APP, "commit": MERGE, "platform": "backend", "changed": [], "app": None, "gate": {},
         "deploy": [{"env": "staging", "source": PUBLIC_SRC, "tag": "main-c66d2f7",
                     "deployed_commit": "c66d2f7", "error": None, "included": True}]}
    clause = bt.check_clause(t, "now", {"hosts": [HOST], "jobs": False})
    assert f"read the image tag in {PUBLIC_SRC}" in clause
    assert "rule AJ" not in clause and "Confirm which" not in clause and "Not this check" not in clause
