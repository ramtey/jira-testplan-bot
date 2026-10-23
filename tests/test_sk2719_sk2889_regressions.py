"""Eight generator flaws, pinned to SK-2719 (plan 1) and SK-2889 (plan 2).

Two UAT runs on 2026-10-23 found the plans wrong in ways none of which is
specific to those tickets:

1. SK-2719 said mobile-api has no CI, and argued from it that its unit tests
   prove nothing. A Codefresh pipeline in the repo and an Argo pipeline in the
   deployments repo both run `npm test`.
2. Its backend deploy check named the iOS merge commit (50aff26), not
   mobile-api's (e2f4229).
3. It required one exact image tag; integ ran a later master build that
   contained the merge, which is correct.
4. Every SK-2889 case (an iOS-only change) began by checking a container image
   or an ArgoCD app.
5. SK-2889 kept two cases it called unreachable as runnable cases.
6. ...and one it called pre-existing.
7. Its twelve regression lines were all labelled unverified assumptions,
   although each named a file the PR changed.
8. SK-2719's client error-handling case covered only JSON error bodies; the
   401 for an invalid token arrives with an empty body.

tests/fixtures/sk2719 and sk2889 are those plans as the generator returned
them, and the trees, files and deployments the stages read, sanitized for
this public repo. The tests below run each stage on them and then the whole
pipeline with a stub model. evals/replay_store_plans.py runs the same stages
on the stored plans in the local bot store.
"""

from __future__ import annotations

import copy
import json
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from src.app import build_targets as bt
from src.app import case_disposition as cd
from src.app import evidence_checks as ev
from src.app import reachability_checks as rc
from src.app.deploy_state import read_deploy_state
from src.app.error_variants import require_error_body_variants
from src.app.models import GenerateTestPlanRequest, TestPlan
from src.app.regression_grounding_critic import diff_hits_for_line
from src.app.services import plan_service
from src.app.services import progress_key as pk

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load(name: str) -> tuple[dict, dict]:
    d = FIXTURES / name
    return json.loads((d / "inputs.json").read_text()), json.loads((d / "plan_v1.json").read_text())


SK2719, PLAN1 = _load("sk2719")
SK2889, PLAN2 = _load("sk2889")

MOBILE_API, IOS, DEPLOYMENTS = "acme/mobile-api", "acme/ios-client", "acme/acme-deployments"
API_MERGE, IOS_MERGE_2719, IOS_MERGE_2889 = "e2f4229", "50aff26", "b485dad"


class FakeGitHub:
    """The repos at their merge commits and the deployments repo at HEAD."""

    def __init__(self, inputs: dict):
        self.inputs = inputs

    async def list_tree(self, repo, ref):
        if repo == DEPLOYMENTS:
            return list(self.inputs["deployments_tree"]) or None
        tree = self.inputs["repo_trees"].get(repo)
        return list(tree) if tree is not None else None

    async def fetch_files_at_ref(self, repo, ref, paths, max_bytes=300_000):
        files = self.inputs["deployments_files"] if repo == DEPLOYMENTS else self.inputs["repo_files"].get(repo, {})
        return {p: files[p] for p in paths if p in files}

    async def compare_commits(self, repo, base, head):
        status = self.inputs["compare"].get(bt.short_name(repo), {}).get(f"{base[:7]}:{head[:7]}")
        return {"status": status, "ahead_by": 1, "behind_by": 0} if status else None


def _plan(fixture: dict) -> TestPlan:
    f = copy.deepcopy(fixture)
    return TestPlan(happy_path=f["happy_path"], edge_cases=f["edge_cases"],
                    regression_checklist=f["regression_checklist"],
                    integration_tests=f["integration_tests"],
                    security_negative_tests=f["security_negative_tests"], risks_and_gaps=[])


async def _build_context(inputs: dict):
    gh = FakeGitHub(inputs)
    prs = copy.deepcopy(inputs["pull_requests"])
    deployments = await bt.discover_deployments(gh, prs)
    evidence = await ev.gather_repo_evidence(gh, prs, "", deployments)
    deploy_state = await read_deploy_state(gh, prs, deployments["deploy_sources"])
    targets = bt.build_targets(prs, evidence, deploy_state, deployments["deployments_repos"])
    return deployments, evidence, deploy_state, targets


def _text(case: dict) -> str:
    return " ".join([case.get("preconditions") or "", case.get("environment") or "", *case.get("steps", [])])


# ── 1. CI from every source; "unknown" when none is found ────────────────────

@pytest.mark.asyncio
async def test_ci_is_found_in_the_repos_codefresh_file_and_the_deployments_repos_argo_pipeline():
    deployments, evidence, _, _ = await _build_context(SK2719)
    assert deployments["deployments_repos"] == [DEPLOYMENTS], "the ticket's own deployments PR names it"
    [pipeline] = deployments["pipelines"][MOBILE_API]
    assert pipeline["source"] == f"{DEPLOYMENTS}/mobile/pipelines/mobile-api/values.yaml"

    api = next(e for e in evidence if e["repo"] == MOBILE_API)
    assert all(e["repo"] != DEPLOYMENTS for e in evidence), "nothing a tester runs is built from it"
    suites = api["build"]["suites"]
    assert suites and all(s["status"] == "runs" for s in suites.values())
    why = next(iter(suites.values()))["why"]
    assert "Codefresh deployment/codefresh/pipeline.yaml runs `- npm test`" in why
    assert "Argo Workflows acme/acme-deployments/mobile/pipelines/mobile-api/values.yaml runs `npm test`" in why
    kinds = {f["kind"] for f in api["build"]["findings"]}
    assert "tests_not_run_in_ci" not in kinds and "ci_runs_tests" in kinds
    # What plan 1 said instead:
    assert PLAN1["stored_build_and_ci"][0]["kind"] == "tests_not_run_in_ci"


@pytest.mark.asyncio
async def test_finding_no_ci_anywhere_is_unknown_not_none():
    inputs = copy.deepcopy(SK2719)
    del inputs["repo_files"][MOBILE_API]["deployment/codefresh/pipeline.yaml"]
    inputs["repo_trees"][MOBILE_API].remove("deployment/codefresh/pipeline.yaml")
    inputs["deployments_tree"] = [p for p in inputs["deployments_tree"] if "/pipelines/" not in p]
    _, evidence, _, _ = await _build_context(inputs)
    api = next(e for e in evidence if e["repo"] == MOBILE_API)
    statuses = {s["status"] for s in api["build"]["suites"].values()}
    assert statuses == {"unknown"}
    assert "no CI pipeline was found" in next(iter(api["build"]["suites"].values()))["why"]
    assert "acme/acme-deployments" in next(iter(api["build"]["suites"].values()))["why"]
    assert not any(f["kind"] == "tests_not_run_in_ci" for f in api["build"]["findings"])


@pytest.mark.asyncio
async def test_a_case_arguing_from_an_unconfirmed_ci_claim_is_marked():
    """integration_tests:5 of plan 1 (here [3]) repeated the false fact, and
    named .github/workflows and Jenkinsfile only as missing — which the CI
    claim check used to accept as a citation."""
    _, evidence, _, _ = await _build_context(SK2719)
    plan = _plan(PLAN1)
    rc.flag_ci_claims(plan, evidence)
    assert plan.integration_tests[3]["expected"].startswith("Contradicted by the CI config")

    plan = _plan(PLAN1)
    flagged = rc.flag_ci_claims(plan, [])
    assert {"location": "integration_tests:3", "kind": "unconfirmed"} in flagged


# ── 2–3. Each repo's own merge commit, checked by containment ────────────────

@pytest.mark.asyncio
async def test_each_case_checks_the_merge_commit_of_the_repo_it_exercises():
    _, _, _, targets = await _build_context(SK2719)
    assert {(t["repo"], t["commit"][:7], t["platform"]) for t in targets} == {
        (MOBILE_API, API_MERGE, "backend"), (IOS, IOS_MERGE_2719, "ios")}
    plan = _plan(PLAN1)
    ev.enforce_deploy_precondition(plan, targets=targets, when="2026-10-23 18:27 UTC")

    backend = plan.integration_tests[0]["steps"][0]          # backend_http, mobile-api files only
    assert API_MERGE in backend and IOS_MERGE_2719 not in backend
    # mobile: cites only iOS files, but its own deploy step named the
    # mobile-api deployment, so both builds are checked, the app's first.
    app = plan.happy_path[0]["steps"][0]
    assert app.startswith("Build check: (1) acme/ios-client (iOS app") and IOS_MERGE_2719 in app
    assert f"(2) acme/mobile-api — confirm the deployed image contains merge commit {API_MERGE}" in app
    both = plan.integration_tests[1]["steps"][0]              # API call + compare in the app
    assert API_MERGE in both and IOS_MERGE_2719 in both


@pytest.mark.asyncio
async def test_the_deploy_check_accepts_any_build_that_contains_the_merge():
    _, _, deploy_state, targets = await _build_context(SK2719)
    integ = next(e for e in deploy_state if e["env"] == "integ-west")
    assert integ["tag"] == "master-6447b19" and integ["prs"][0]["included"] is True

    plan = _plan(PLAN1)
    case = plan.integration_tests[0]
    assert "SK-2719-in-person-session-4a6351a" in _text(case), "the fixture carries the pinned tag"
    ev.enforce_deploy_precondition(plan, targets=targets, when="2026-10-23 18:27 UTC")
    step = case["steps"][0]
    assert f"compare/{API_MERGE}...<deployed commit>" in step
    assert "Any later build that contains it passes" in step
    assert "integ-west runs master-6447b19, which contains it" in step
    assert "acme/acme-deployments/mobile/mobile-api/integ-west/values.yaml" in step
    assert "SK-2719-in-person-session-4a6351a" not in _text(case)
    assert case["deploy_check_replaced"] is True, "the model's own exact-tag step is replaced in place"

    banner = ev.build_deployment_status(deploy_state, None, [t["repo"] for t in targets], "now", targets=targets)
    assert "integ-west" in banner["deployed_envs"]


# ── 4. The check fits the platform ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_ios_cases_check_the_installed_build_never_an_image():
    _, evidence, deploy_state, targets = await _build_context(SK2889)
    [ios] = evidence
    assert ios["platform"] == "ios"
    assert ios["app_version"] == {"version": "2.13.0", "build": "1", "bundle_ids": ["com.acme.mobile.integ"],
                                  "file": "Acme.xcodeproj/project.pbxproj"}
    plan = _plan(PLAN2)
    ev.enforce_deploy_precondition(plan, targets=targets, when="now")
    checked = [c for s in ("happy_path", "edge_cases", "integration_tests") for c in getattr(plan, s)
               if c.get("deploy_check_added") or c.get("deploy_check_replaced")]
    assert len(checked) >= 10
    for case in checked:
        step = case["steps"][0]
        for needed in ("CFBundleShortVersionString must be at least 2.13.0", "CFBundleVersion",
                       "plutil -p", "xcrun simctl listapps booted", "com.acme.mobile.integ",
                       f"git merge-base --is-ancestor {IOS_MERGE_2889} HEAD"):
            assert needed in step, needed
        assert "ArgoCD app's image" not in step and "running pod" not in step

    banner = ev.build_deployment_status(deploy_state, None, [IOS], "now", targets=targets)
    assert banner["warnings"] == [
        "acme/ios-client is an iOS app, so there is no deployment to read: every case on it starts by "
        "checking that the installed ios-client app is built from a commit containing b485dad "
        "(version 2.13.0 or later)."]
    assert "no deployment source is configured" in PLAN2["stored_deployment_status"]["warnings"][0]


# ── 5–7. What a tester cannot grade leaves the checklist ─────────────────────

def test_unreachable_and_pre_existing_cases_leave_the_numbered_sections():
    plan = _plan(PLAN2)
    titles = [c["title"] for c in plan.edge_cases]
    out = cd.route_out_of_uat(plan, dev_infos=[{"pull_requests": SK2889["pull_requests"]}])

    assert [c["title"] for c in out[cd.CODE_REVIEW_KEY]] == [titles[0], titles[4]]
    for entry in out[cd.CODE_REVIEW_KEY]:
        assert entry["origin_section"] == "edge_cases"
        assert entry["why_not_uat"] and entry["suggested_test"].startswith("Add a unit test")
        assert not entry["what_to_verify"].startswith("Not reachable")
    [pre] = out[cd.PRE_EXISTING_KEY]
    assert pre["title"] == titles[3] and pre["note"].startswith("EditSplitView")
    assert [c["title"] for c in plan.edge_cases] == [titles[1], titles[2], titles[5]]
    assert out["moved"] == ["edge_cases:0", "edge_cases:3", "edge_cases:4", "regression_checklist:13"]


def test_regression_lines_cite_the_changed_code_or_leave_the_core_checklist():
    plan = _plan(PLAN2)
    lines = list(plan.regression_checklist)
    out = cd.route_out_of_uat(plan, dev_infos=[{"pull_requests": SK2889["pull_requests"]}])
    assert plan.regression_checklist == lines[:13]
    [low] = out[cd.LOW_CONFIDENCE_KEY]
    assert low["line"] == lines[13] and "cites no file the change touched" in low["why"]

    # And the critic is shown the diff of the file a line cites, so a line
    # about SceneDelegate is no longer "no code shows this".
    [hit] = diff_hits_for_line(lines[0], [{"development_info": {"pull_requests": SK2889["pull_requests"]}}])
    assert hit["path"] == "Acme/Classes/SceneDelegate.swift" and hit["content"]


def test_old_plans_keep_their_key_and_new_plans_count_only_runnable_cases():
    """Shape 1 (stored plans) is untouched: plan 2's key is still 6-6-5-14.
    A plan routed by case_disposition counts what a tester can grade."""
    old = {k: PLAN2[k] for k in ("happy_path", "edge_cases", "integration_tests", "regression_checklist")}
    assert pk.build_progress_key(["SK-2889"], old) == PLAN2["stored_progress_key"]

    plan = _plan(PLAN2)
    out = cd.route_out_of_uat(plan, dev_infos=[{"pull_requests": SK2889["pull_requests"]}])
    body = {"happy_path": plan.happy_path, "edge_cases": plan.edge_cases,
            "integration_tests": plan.integration_tests, "regression_checklist": plan.regression_checklist,
            **{k: out[k] for k in cd.OUT_OF_UAT_KEYS}, "plan_shape": cd.PLAN_SHAPE}
    assert pk.build_progress_key(["SK-2889"], body) == "SK-2889:6-3-5-13"
    assert not any(i.startswith(cd.OUT_OF_UAT_KEYS) for i in pk.case_index(body))


def test_a_case_moved_out_is_reported_by_where_it_went_on_regeneration():
    from src.app.services.plan_revision import map_case_ids

    old = {k: PLAN2[k] for k in ("happy_path", "edge_cases", "integration_tests", "regression_checklist")}
    plan = _plan(PLAN2)
    out = cd.route_out_of_uat(plan, dev_infos=[{"pull_requests": SK2889["pull_requests"]}])
    new = {"happy_path": plan.happy_path, "edge_cases": plan.edge_cases,
           "integration_tests": plan.integration_tests, "regression_checklist": plan.regression_checklist,
           **{k: out[k] for k in cd.OUT_OF_UAT_KEYS}}
    status = {m["from"]: m["status"] for m in map_case_ids(old, new)}
    assert status["edge_cases:0"] == "code_review" and status["edge_cases:4"] == "code_review"
    assert status["edge_cases:3"] == "pre_existing"
    assert status["regression_checklist:13"] == "low_confidence"


# ── 8. Error handling covers empty and non-JSON bodies ───────────────────────

def test_a_client_error_handling_case_gets_the_empty_and_non_json_variants():
    plan = _plan(PLAN1)
    case = plan.integration_tests[2]
    assert "empty" not in json.dumps(case).lower()
    changed = require_error_body_variants(plan)
    assert "integration_tests:2" in changed
    step = case["steps"][-1]
    assert step.startswith("Error-body variants") and "EMPTY body" in step and "NON-JSON body" in step
    assert require_error_body_variants(plan) == [], "idempotent"
    assert case["error_body_variants_added"] == ["empty", "non_json"]


def test_a_case_that_is_not_about_client_error_handling_gets_no_variant_step():
    plan = _plan(PLAN1)
    before = copy.deepcopy(plan.integration_tests[1])
    require_error_body_variants(plan)
    assert plan.integration_tests[1] == before


# ── The whole pipeline, with a stub model ────────────────────────────────────

async def _replay(inputs: dict, fixture: dict, key: str) -> dict:
    gh = FakeGitHub(inputs)
    plan = _plan(fixture)

    class _LLM:
        async def generate_test_plan(self, **kwargs):
            return plan

        async def verify_unit_coverage(self, surviving):
            return {}

    async def _noop(*a, **k):
        return None

    async def _discover(dev_info):
        return await bt.discover_deployments(gh, dev_info["pull_requests"])

    async def _repo(dev_info, mentioned, deployments=None):
        return await ev.gather_repo_evidence(gh, dev_info["pull_requests"], mentioned, deployments)

    async def _deploy(dev_info, deployments=None):
        return await read_deploy_state(gh, dev_info["pull_requests"], (deployments or {}).get("deploy_sources"))

    request = GenerateTestPlanRequest(
        ticket_key=key, summary=f"{key} fixture", description="Fixture replay.", issue_type="Story",
        development_info={"pull_requests": copy.deepcopy(inputs["pull_requests"])},
    )
    with (
        patch.object(plan_service, "get_llm_client", return_value=_LLM()),
        patch.object(plan_service.JiraClient, "download_image_as_base64", AsyncMock(return_value=None)),
        patch.object(plan_service, "classify_deliverable", _noop),
        patch.object(plan_service, "run_grounding_critic", _noop),
        patch.object(plan_service, "run_code_grounding_critic", _noop),
        patch.object(plan_service, "run_fix_scope_critic", _noop),
        patch.object(plan_service, "run_surface_mismatch_critic", _noop),
        patch.object(plan_service, "run_regression_grounding_critic", _noop),
        patch.object(plan_service, "_discover_deployments", _discover),
        patch.object(plan_service, "_read_repo_evidence", _repo),
        patch.object(plan_service, "_read_deploy_state", _deploy),
        patch.object(plan_service.settings, "deploy_state_sources", {}),
    ):
        return await plan_service.generate_single(request)


@pytest.mark.asyncio
async def test_sk2889_regenerated_has_ios_build_checks_and_only_runnable_cases():
    response = await _replay(SK2889, PLAN2, "SK-2889")
    assert response["plan_shape"] == 2
    assert len(response["code_review_checks"]) == 2 and len(response["pre_existing_behaviour"]) == 1
    assert len(response["regression_low_confidence"]) == 1
    assert pk.fingerprint(response) == "6-3-5-13"
    for c in response["happy_path"] + response["edge_cases"] + response["integration_tests"]:
        if c.get("deploy_check_added") or c.get("deploy_check_replaced"):
            assert "CFBundleShortVersionString" in c["steps"][0] and "ArgoCD app's image" not in c["steps"][0]
    assert response["source_provenance"]["build_targets"] == [
        {"repo": IOS, "commit": "b485dad1be95a8662635e1a5a72dceb4d73f322d", "platform": "ios",
         "app_version": "2.13.0"}]


@pytest.mark.asyncio
async def test_sk2719_regenerated_has_ci_per_repo_checks_and_error_variants():
    response = await _replay(SK2719, PLAN1, "SK-2719")
    kinds = {f["kind"] for f in response["build_and_ci"] if f.get("repo") == MOBILE_API}
    assert "ci_runs_tests" in kinds and "tests_not_run_in_ci" not in kinds
    assert response["source_provenance"]["deployments_discovered"]["pipelines"] == {
        MOBILE_API: [f"{DEPLOYMENTS}/mobile/pipelines/mobile-api/values.yaml"]}
    integ = next(c for c in response["integration_tests"] if c["title"].startswith("In-person session endpoint"))
    assert API_MERGE in integ["steps"][0] and IOS_MERGE_2719 not in integ["steps"][0]
    assert "SK-2719-in-person-session-4a6351a" not in _text(integ)
    errors = next(c for c in response["integration_tests"] if c["title"].startswith("Documented downstream"))
    assert errors["steps"][-1].startswith("Error-body variants")
    # The local-jest case the model wrote because "it cannot be produced from
    # the app" is a code-review check, not a UAT case.
    assert any(e["title"].startswith("[Backend] 502 branch") for e in response["code_review_checks"])
    assert "integ-west" in response["deployment_status"]["deployed_envs"]
