"""Deployment state comes from a deployment read, never from an old ticket comment.

Why this file exists
--------------------
SK-3059 (plan 704 v1, regenerated as plan 747 v2 on 2026-11-19). The repo
ships as images named ``skysights-api`` and ``skysights-admin-ui``; only the
pipeline trigger in the deployments repo names the repo. Discovery matched
values directories by repo name, found none, and the generator fell back to
the newest ticket comment ("#100 is not deployed yet"). The regenerated plan
said ``not_deployed`` and marked three runnable cases "BLOCKED BY SETUP: PR
#100 is not deployed (comment 346733)" while integ already ran a build that
contained #100. The build check also called the change a "web app" when it
ships in the API container. SK-2835 (plan 748) hit the same discovery gap.

The fixture in tests/fixtures/sk3059 is that layout and those cases, with
hosts, the registry, ids and names removed (see its README).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.app import build_targets as bt
from src.app import deployed_env_checks as dec
from src.app import evidence_checks as ev
from src.app.deploy_state import read_deploy_state, render_deploy_state
from src.app.services.test_plan_generator import deploy_context

FIX = Path(__file__).parent / "fixtures" / "sk3059"
CASES = json.loads((FIX / "plan_747_cases.json").read_text())

APP = "example-org/broker-wrapped"
DEPLOY = "example-org/app-deployments"
MERGE_99 = "0e941af312567ea7894b93af8314157c1054a5b7"
MERGE_100 = "8b5eb66346925d1d23683a970a01a3526be0f8e0"
WHEN = "2026-11-19 18:00 UTC"

FILES = {
    f"{DEPLOY}/mobile/pipelines/skysights-api/values.yaml": (FIX / "pipeline_skysights_api.yaml").read_text(),
    f"{DEPLOY}/mobile/pipelines/skysights-admin-ui/values.yaml":
        (FIX / "pipeline_skysights_admin_ui.yaml").read_text(),
    f"{DEPLOY}/mobile/skysights-api/integ-east/values.yaml": (FIX / "skysights_api_integ_values.yaml").read_text(),
    f"{DEPLOY}/mobile/skysights-api/staging-east/values.yaml":
        (FIX / "skysights_api_staging_values.yaml").read_text(),
    f"{DEPLOY}/mobile/skysights-admin-ui/integ-east/values.yaml":
        (FIX / "skysights_admin_ui_integ_values.yaml").read_text(),
}
TREE = [p.split("/", 2)[2] for p in FILES] + [
    "mobile/skysights-api/Chart.yaml", "mobile/skysights-api/values.yaml",
    "mobile/other-service/integ-east/values.yaml", "mobile/pipelines/other-service/values.yaml",
]

PRS = [
    {"repository": APP, "number": 99, "url": "u99", "status": "MERGED", "head_sha": "h99",
     "merge_commit_sha": MERGE_99, "merged_at": "2026-11-10T22:56:00Z", "source_branch": "SK-3059-cves",
     "files_changed": [{"filename": f} for f in (
         "apps/broker-wrapped-api/package.json", "apps/functions/package.json",
         "apps/skysights-admin-ui/package.json", "pnpm-lock.yaml")]},
    {"repository": APP, "number": 100, "url": "u100", "status": "MERGED", "head_sha": "h100",
     "merge_commit_sha": MERGE_100, "merged_at": "2026-11-13T19:56:03Z", "source_branch": "SK-3059-codeql",
     "files_changed": [{"filename": f} for f in (
         "apps/broker-wrapped-api/Dockerfile", "apps/broker-wrapped-api/src/routes/cron.ts",
         "apps/broker-wrapped-api/src/services/tts-service.ts", "apps/functions/package.json",
         "package.json", "pnpm-lock.yaml")]},
]
# Newest comment: "#100 is not deployed yet".
COMMENTS = [
    {"id": "346224", "created": "2026-11-11T14:01:59.000-0800",
     "body": "Quick update: #99 is now live in prod (release1.5.0-0e941af)."},
    {"id": "346733", "created": "2026-11-13T11:56:55.000-0800",
     "body": "Merged #100 (remaining patch-level dependency and CodeQL alerts). It is not deployed yet."},
]
# What the model wrote into evidence_claims on plan 747.
MODEL_STATUS = {"deployed": "no",
                "statement": "PR #100 (merge commit 8b5eb66) has merged but is not deployed, so the "
                             "log-line and Dockerfile changes are not running anywhere yet",
                "source": "comment 346224, comment 346733"}

# Ancestry in the repo: integ's 7c754c2 and aa91729 contain both merges;
# staging's 0e941af is #99's merge itself, which predates #100.
CONTAINS = {("0e941af", "7c754c2"), ("8b5eb66", "7c754c2"), ("0e941af", "aa91729"),
            ("8b5eb66", "aa91729"), ("0e941af", "0e941af")}


class FakeGitHub:
    def __init__(self, search_fails: bool = False):
        self.searches: list[tuple[str, str]] = []
        self.search_fails = search_fails

    async def list_tree(self, repo, ref):
        return list(TREE) if repo == DEPLOY else None

    async def search_code_hits(self, query, scope, max_hits=8):
        self.searches.append((query, scope))
        if self.search_fails:
            raise RuntimeError("throttled")
        return [{"repo": DEPLOY, "path": p.split("/", 2)[2], "fragments": []}
                for p, text in FILES.items() if "/pipelines/" in p and query.strip('"') in text]

    async def fetch_files_at_ref(self, repo, ref, paths):
        return {p: FILES[f"{repo}/{p}"] for p in paths if f"{repo}/{p}" in FILES}

    async def compare_commits(self, repo, base, head):
        if (base[:7], head[:7]) in CONTAINS:
            return {"status": "identical" if base[:7] == head[:7] else "ahead", "behind_by": 0}
        return {"status": "behind", "behind_by": 3}

    async def latest_commit_for_path(self, repo, path):
        return {"sha": "d" * 40, "date": "2026-11-18T22:13:44Z"}


def _plan():
    p = copy.deepcopy(CASES)
    return SimpleNamespace(happy_path=p["happy_path"], edge_cases=p["edge_cases"],
                           integration_tests=p["integration_tests"], security_negative_tests=[],
                           regression_checklist=[], shared_preconditions=[])


async def _run(github=None, model_status=MODEL_STATUS):
    """Discovery → deploy state → build check → banner → re-check, as plan_service runs them."""
    github = github or FakeGitHub()
    prs = copy.deepcopy(PRS)
    found = await bt.discover_deployments(github, prs, deployments_repos=[DEPLOY])
    state = await read_deploy_state(github, prs, found["deploy_sources"],
                                    services=found["deploy_services"])
    targets = bt.build_targets(prs, [{"repo": APP, "platform": "web"}], state, found["deployments_repos"])
    plan, top = _plan(), {}
    ev.enforce_deploy_precondition(plan, targets=targets, when=WHEN, top=top)
    banner = ev.build_deployment_status(state, model_status, [APP], WHEN, targets=targets, comments=COMMENTS)
    report: dict = {}
    dec.recheck_before_save(plan, report, deploy_context=deploy_context(state, prs, COMMENTS, WHEN))
    return found, state, plan, top, banner, report


def _assert_no_deployment_block(plan):
    from src.app.plan_recheck import _sentences

    for cid, c in _cases(plan):
        assert not dec._is_deploy_claim(c.get("blocked_by_setup") or ""), (cid, c.get("blocked_by_setup"))
        for sent in _sentences(c.get("preconditions") or ""):
            if dec._BLOCK_SENTENCE_RE.search(sent):
                assert not dec._is_deploy_claim(sent), (cid, sent)


def _cases(plan):
    for s in ("happy_path", "edge_cases", "integration_tests"):
        for i, c in enumerate(getattr(plan, s)):
            yield f"{s}:{i}", c


# ---------------------------------------------------------------------------
# The regression: SK-3059 plan 747 v2
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sk3059_comment_says_not_deployed_but_the_deployments_repo_shows_it_is():
    found, state, plan, top, banner, report = await _run()

    # 1. The deployments repo is the source: found through the pipeline trigger.
    assert found["deploy_sources"][APP]["integ-east (skysights-api)"] == \
        f"{DEPLOY}/mobile/skysights-api/integ-east/values.yaml"
    integ_api = next(e for e in state if e["env"] == "integ-east (skysights-api)")
    assert integ_api["tag"] == "release1.6.0-7c754c2"
    assert [p["included"] for p in integ_api["prs"]] == [True, True]

    # 2. The banner is not "not_deployed", and the comment is dated, not a status.
    assert banner["status"] != "not_deployed"
    assert banner["status"] == "deployed"
    assert "integ-east (skysights-api)" in banner["deployed_envs"]
    claim = next(w for w in banner["warnings"] if "ticket comment" in w)
    assert claim.startswith("As of 2026-11-13, a ticket comment said:")
    assert "point-in-time claim" in claim and "Superseded" in claim
    assert not any("no deployment source" in w for w in banner["warnings"])
    # Staging runs #99 but not #100: said, as a read, not as a block.
    assert any(w.startswith("Partly deployed to staging-east (skysights-api)") and "#100" in w
               for w in banner["warnings"])

    # 3. No case carries a deployment BLOCKED BY SETUP.
    _assert_no_deployment_block(plan)
    # Only happy_path:0 stays blocked, on its unconfirmed admin token (not deployment).
    blocked = [cid for cid, c in _cases(plan) if "BLOCKED BY SETUP" in (c.get("preconditions") or "")]
    assert blocked == ["happy_path:0"]
    assert "Admin test token" in plan.happy_path[0]["blocked_by_setup"]
    hp0 = plan.happy_path[0]
    assert "Build check passed" in hp0["preconditions"]           # the gate stays
    assert "as of 2026-11-13" in hp0["preconditions"].lower()     # the claim is dated
    assert "older than the newest deployments-repo commit" in hp0["preconditions"]
    # A setup need that is not deployment (the admin token) is still open.
    assert any(s.get("status") == "unconfirmed" for s in hp0.get("required_setup") or [])
    released = [f for f in report["findings"] if f["rule"] == "AF" and f["action"] == "released"]
    assert len(released) == 4

    # 4. The build check names the API container that ships the change.
    step = top["build_check"]["step"]
    assert "the skysights-api API container (built from apps/broker-wrapped-api/Dockerfile)" in step
    assert "(web app)" not in step
    assert "mobile/skysights-api/integ-east/values.yaml" in step


@pytest.mark.asyncio
async def test_prompt_names_the_image_and_whether_it_ships_the_change():
    _, state, *_ = await _run()
    text = render_deploy_state(state)
    assert "image skysights-api (API container, built from apps/broker-wrapped-api/Dockerfile; ships" in text
    assert "not a deployment read" not in text  # guidance lives in DEPLOY_STATE_GUIDANCE


# ---------------------------------------------------------------------------
# No source readable: unknown, and still no deployment block
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_without_any_readable_source_status_is_unknown_and_no_case_is_blocked():
    found, state, plan, top, banner, report = await _run(FakeGitHub(search_fails=True))
    assert state == []
    assert any("could not search" in e for e in found["errors"])
    assert banner["status"] == "unknown"
    _assert_no_deployment_block(plan)
    # With no manifest matched, the diff still says what ships the change.
    step = top["build_check"]["step"]
    assert "apps/broker-wrapped-api (API container)" in step
    assert "(web app)" not in step


# ---------------------------------------------------------------------------
# A real read that shows the change missing keeps the block, citing the read
# ---------------------------------------------------------------------------

def test_a_deployment_read_that_lacks_the_change_everywhere_keeps_the_block():
    case = copy.deepcopy(CASES["edge_cases"][0])
    ctx = {"entries": [{"env": "integ", "tag": "main-0000000", "source": "x/y/integ/values.yaml",
                        "included": False, "ships": True}],
           "merges": [], "comment_dates": {}, "when": WHEN}
    report: dict = {}
    assert dec.release_deploy_blocks(case, "edge_cases", ctx, report)
    assert "not deployed per the deployment read" in case["blocked_by_setup"]
    assert "main-0000000" in case["blocked_by_setup"]


def test_an_image_that_carries_no_changed_file_is_not_where_the_change_runs():
    assert bt.ships_change("apps/skysights-admin-ui", ["apps/broker-wrapped-api/src/a.ts",
                                                       "docs/threat-model.md"]) is False
    assert bt.ships_change("apps/broker-wrapped-api", ["apps/broker-wrapped-api/src/a.ts"]) is True
    assert bt.ships_change("apps/skysights-admin-ui", ["pnpm-lock.yaml"]) is None
    assert bt.deployable_kind("skysights-api", "apps/broker-wrapped-api") == "api"
    assert bt.deployable_kind("skysights-admin-ui", "apps/skysights-admin-ui") == "web"
    assert bt.deployable_kind("apps/functions") == "functions"


@pytest.mark.asyncio
async def test_a_repo_whose_values_dirs_carry_its_name_never_searches():
    gh = FakeGitHub()
    prs = [{**PRS[1], "repository": "example-org/skysights-api"}]
    found = await bt.discover_deployments(gh, prs, deployments_repos=[DEPLOY])
    assert found["deploy_sources"]["example-org/skysights-api"]
    assert gh.searches == []


@pytest.mark.parametrize("text,is_deploy", [
    ("PR #100 is not deployed (comment 346733).", True),
    ("Needs PR #100 deployed and scanned.", True),
    ("It is not confirmed that functions have been deployed since this change.", True),
    ("PR #100 deployed to integ", True),
    # Accounts and data on a deployed environment are setup, and stay blocked.
    ("a test account on the deployed integ environment", False),
    ("second tenant in the deployment's org", False),
    ("deactivated user", False),
    # Plan 728: a PR-state fact and a person's approval are not deployment state.
    ("PR #31 is not merged or deployed; owner the mobile team.", False),
    ("Lead approval for the ingestion deploy", False),
])
def test_only_deployment_of_the_change_counts_as_a_deployment_block(text, is_deploy):
    assert dec._is_deploy_claim(text) is is_deploy


def test_a_real_setup_block_on_a_deployed_environment_is_kept():
    case = {"title": "t", "preconditions": "", "steps": ["s"],
            "blocked_by_setup": "a test account on the deployed integ environment (ask QA)"}
    plan = SimpleNamespace(happy_path=[case], edge_cases=[], integration_tests=[], security_negative_tests=[],
                           regression_checklist=[])
    dec.check_setup(plan, {}, {"entries": [], "merges": [], "comment_dates": {}, "when": WHEN})
    assert case["preconditions"].startswith("BLOCKED BY SETUP (rule AF): a test account")


def test_a_shared_dockerfile_monorepo_matches_the_app_directory_by_service_name():
    changed = ["apps/constellation-mls-migration/src/main.ts", "libs/listing/src/importer.ts"]
    assert bt.ships_change(None, changed, "constellation-mls-migration") is True
    assert bt.ships_change(None, changed, "bridge-copy-v3") is None


def test_many_candidate_images_are_summarised_not_listed():
    t = {"changed": [], "deploy": [{"service": f"svc-{i}", "kind": "service", "ships": None} for i in range(6)]}
    label = bt.deployable_label(t)
    assert label.startswith("one of the 6 images") and "svc-0, svc-1, svc-2 and 3 more" in label


def test_status_is_never_not_deployed_while_some_environment_runs_the_change():
    state = [{"repo": "a/x", "env": "integ", "tag": "t1", "prs": [{"number": 1, "included": True}]},
             {"repo": "a/x", "env": "prod", "tag": "t0", "prs": [{"number": 1, "included": False}]}]
    targets = [{"repo": "a/x", "platform": "backend"}, {"repo": "a/unread", "platform": "backend"}]
    banner = ev.build_deployment_status(state, None, [], WHEN, targets=targets)
    assert banner["status"] == "unknown"     # a/unread could not be read
    banner = ev.build_deployment_status(state, None, [], WHEN, targets=targets[:1])
    assert banner["status"] == "deployed"
