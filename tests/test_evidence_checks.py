"""Evidence rules A–F, pinned to SK-2826 (plan 608, v1).

Plan 608 read well and was wrong in six ways that had nothing to do with that
ticket. The fixture in tests/fixtures/sk2826/ is the real input — ticket
description, all three comments, both PR diffs, the test file, the Dockerfile,
the repo tree and the integ deployment that still ran the commit before the
fix — sanitized for this public repo. `plan608_v1.json` is what the generator
returned for it.

These tests replay that plan through the pipeline with a stub model, so they
pin the parts that do not depend on the model following the prompt: the
evidence that reaches the prompt, and what code does with the plan afterwards.
`evals/sk2826_evidence.py` runs the same inputs through the real model.
"""

from __future__ import annotations

import copy
import json
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from src.app import evidence_checks as ev
from src.app.models import GenerateTestPlanRequest, TestPlan
from src.app.services import plan_service

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "sk2826"
INPUTS = json.loads((FIXTURE / "inputs.json").read_text())
V1 = json.loads((FIXTURE / "plan608_v1.json").read_text())

TESTING_NOTE = "336173"
UNIT_TEST_FILE = "__tests__/request-processor.unit.js"


class FakeGitHub:
    """The repo at the merge commit, from the fixture. Nothing else exists."""

    async def list_tree(self, repo, ref):
        return list(INPUTS["repo_tree"])

    async def fetch_files_at_ref(self, repo, ref, paths, max_bytes=300_000):
        return {p: INPUTS["repo_files"][p] for p in paths if p in INPUTS["repo_files"]}


def _v1_plan() -> TestPlan:
    v1 = copy.deepcopy(V1)
    return TestPlan(
        happy_path=v1["happy_path"],
        edge_cases=v1["edge_cases"],
        regression_checklist=v1["regression_checklist"],
        integration_tests=v1["integration_tests"],
        risks_and_gaps=v1["risks_and_gaps"],
    )


def _comments() -> list[dict]:
    # As the Jira filter hands them over: the bot's own plan comment removed.
    return [c for c in INPUTS["comments"] if c["id"] != "336465"]


async def _evidence() -> list[dict]:
    return await ev.gather_repo_evidence(FakeGitHub(), INPUTS["pull_requests"], INPUTS["description"])


def _dev_info(**extra) -> dict:
    return {"pull_requests": copy.deepcopy(INPUTS["pull_requests"]),
            "commits": INPUTS["commits"], **extra}


# ── A: comments ──────────────────────────────────────────────────────────────

def _adf(text: str) -> dict:
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": para}]}
        for para in text.split("\n\n") if para.strip()
    ]}


def test_every_comment_reaches_the_generator_with_its_id_and_the_note_first():
    """The filter used to keep 5 of the last 15 by keyword, with no id, so no
    case could cite the note it came from."""
    from src.app.jira_client import JiraClient

    raw = [{"id": c["id"], "author": {"displayName": c["author"]}, "body": _adf(c["body"]),
            "created": c["created"]} for c in INPUTS["comments"]]
    chatter = [{"id": str(900 + i), "author": {"displayName": "Dev"},
                "body": _adf(f"Checked edge case {i} for the bug"), "created": "2026-10-10"}
               for i in range(12)]
    selected = JiraClient._filter_testing_comments(object.__new__(JiraClient), raw + chatter)

    ids = [c.id for c in selected]
    assert ids[0] in ("336014", TESTING_NOTE) and TESTING_NOTE in ids[:2]
    assert "336014" in ids
    assert "336465" not in ids, "the bot's own plan is never fed back"
    assert len(selected) > 5


def test_the_testing_note_is_a_primary_source():
    notes = {n["id"]: n["kind"] for n in ev.primary_notes(INPUTS["comments"])}
    assert notes == {"336014": "kickoff research", TESTING_NOTE: "testing notes"}


# ── C, D, F: what the repo evidence says ─────────────────────────────────────

@pytest.mark.asyncio
async def test_the_test_file_is_read_whole_at_the_merge_commit():
    """Plan 608 saw no test code: test patches are stripped from the prompt
    and repo-wide discovery returned nothing."""
    [entry] = await _evidence()
    assert entry["ref"] == INPUTS["merge_commit"]
    assert UNIT_TEST_FILE in entry["test_sources"]
    assert "logs the full byte length of the body alongside the excerpt" in entry["test_sources"][UNIT_TEST_FILE]


@pytest.mark.asyncio
async def test_suite_is_not_run_in_ci():
    """No workflow files, and the image build's `npm test` is a no-op."""
    [entry] = await _evidence()
    build = entry["build"]
    assert build["suites"][UNIT_TEST_FILE]["status"] == "not_run"
    finding = next(f for f in build["findings"] if f["kind"] == "tests_not_run_in_ci")
    assert finding["finding"].startswith("Tests exist but are not run in CI")
    assert "exit 0" in finding["finding"]


@pytest.mark.asyncio
async def test_dockerfile_ignores_the_lockfile():
    [entry] = await _evidence()
    finding = next(f for f in entry["build"]["findings"] if f["kind"] == "lockfile_not_shipped")
    assert "webhook-delivery/Dockerfile" in finding["finding"]
    assert "yarn.lock" in finding["finding"] and "npm i" in finding["finding"]
    assert "node-fetch ^2.6.1" in finding["finding"], "the dependency the ticket is about is named"


def test_a_workflow_that_runs_tests_counts_as_ci():
    tree = INPUTS["repo_tree"] + [".github/workflows/ci.yml"]
    files = {**INPUTS["repo_files"], ".github/workflows/ci.yml": "jobs:\n  t:\n    steps:\n      - run: yarn jest\n"}
    build = ev.analyze_build(tree, files, ["utils/request-processor.js"], [UNIT_TEST_FILE])
    assert build["suites"][UNIT_TEST_FILE]["status"] == "runs"


def test_an_unread_workflow_is_unknown_not_absent():
    tree = INPUTS["repo_tree"] + [".github/workflows/ci.yml"]
    build = ev.analyze_build(tree, INPUTS["repo_files"], ["utils/request-processor.js"], [UNIT_TEST_FILE])
    assert build["suites"][UNIT_TEST_FILE]["status"] == "unknown"


@pytest.mark.asyncio
async def test_an_unlistable_repo_is_reported_as_unread():
    class NoTree(FakeGitHub):
        async def list_tree(self, repo, ref):
            return None

    [entry] = await ev.gather_repo_evidence(NoTree(), INPUTS["pull_requests"])
    assert "could not list" in entry["error"]
    assert entry["build"] is None and entry["test_sources"] == {}


# ── E: requirements ──────────────────────────────────────────────────────────

def _req(substr: str) -> dict:
    return next(r for r in ev.extract_requirements(INPUTS["description"]) if substr in r["text"])


def test_requirements_come_from_what_to_change_and_done_when():
    assert _req("explicit byte limit")["source"] == "What to change"
    assert _req("regression test proves the exploit path")["source"] == "Done when"


def test_an_unassessed_requirement_is_still_a_gap():
    reqs = ev.extract_requirements(INPUTS["description"])
    gaps = ev.build_acceptance_gaps(reqs, None)
    assert {g["id"] for g in gaps} == {r["id"] for r in reqs}
    assert all(g["status"] == "not_assessed" for g in gaps)


def test_met_requirements_drop_out_and_partial_ones_stay():
    reqs = ev.extract_requirements(INPUTS["description"])
    checks = [{"id": r["id"], "status": "met", "evidence": "x"} for r in reqs]
    byte = _req("explicit byte limit")["id"]
    test = _req("regression test proves")["id"]
    for c in checks:
        if c["id"] == byte:
            c.update(status="partial", missing="no byte limit", evidence="utils/http.js:7-27")
        if c["id"] == test:
            c.update(status="unmet", missing="no test reproduces the clone stall")
    gaps = ev.build_acceptance_gaps(reqs, checks)
    assert [g["id"] for g in gaps] == [byte, test]
    assert gaps[0]["evidence"] == "utils/http.js:7-27"


# ── C: coverage claims ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_v1_coverage_claims_are_all_reverted():
    """Every v1 claim cites a PR description, and nothing runs the suite."""
    plan = _v1_plan()
    [entry] = await _evidence()
    demoted, surviving = ev.enforce_unit_test_coverage(
        plan, entry["test_sources"], entry["build"]["suites"])
    assert surviving == []
    assert len(demoted) == 6
    assert all("PR description" in d["reason"] for d in demoted)
    assert not plan.happy_path[1]["covered_by_unit_test"], "XML success flag past char 256"
    assert not plan.edge_cases[2]["covered_by_unit_test"], "excerpt in chars, responseBytes in bytes"


def _claim(case: dict, name: str, evidence: str) -> dict:
    case.update(covered_by_unit_test=True, unit_test_ref=f"{UNIT_TEST_FILE} › {name}",
                coverage_evidence=evidence)
    return case


@pytest.mark.asyncio
async def test_a_well_formed_claim_still_needs_ci():
    plan = _v1_plan()
    _claim(plan.edge_cases[2], "logs the full byte length of the body alongside the excerpt",
           "expect(loggedEntry().responseBytes).toBe(LARGE_BODY_BYTES * 2);")
    [entry] = await _evidence()
    demoted, _ = ev.enforce_unit_test_coverage(plan, entry["test_sources"], entry["build"]["suites"])
    reason = next(d["reason"] for d in demoted if d["case_id"] == "edge_cases:2")
    assert "tests exist but are not run in CI" in reason


def test_a_quote_that_is_not_in_the_named_test_is_rejected():
    plan = _v1_plan()
    _claim(plan.happy_path[1], "reads a large SOFTPRO_LEGACY_XML success response directly without cloning",
           "expect(resp.endsWith(MARKER)).toBe(true);")  # from a different test
    demoted, _ = ev.enforce_unit_test_coverage(
        plan, {UNIT_TEST_FILE: INPUTS["repo_files"][UNIT_TEST_FILE]},
        {UNIT_TEST_FILE: {"status": "runs", "why": "ci"}})
    assert "not in" in next(d["reason"] for d in demoted if d["case_id"] == "happy_path:1")


def test_with_ci_the_semantic_check_decides_and_partial_stays_in_the_checklist():
    """Even with CI running the suite, the XML test puts <success> first and
    the multibyte test asserts only the byte count. The coverage critic is
    what catches that; its verdict sends both back with the reason."""
    plan = _v1_plan()
    _claim(plan.happy_path[1], "reads a large SOFTPRO_LEGACY_XML success response directly without cloning",
           "expect(response.text).toHaveBeenCalledTimes(1);")
    _claim(plan.edge_cases[2], "logs the full byte length of the body alongside the excerpt",
           "expect(loggedEntry().responseBytes).toBe(LARGE_BODY_BYTES * 2);")
    for c in (plan.happy_path[0], plan.happy_path[2], plan.happy_path[3], plan.edge_cases[1]):
        c["covered_by_unit_test"] = False
    demoted, surviving = ev.enforce_unit_test_coverage(
        plan, {UNIT_TEST_FILE: INPUTS["repo_files"][UNIT_TEST_FILE]},
        {UNIT_TEST_FILE: {"status": "runs", "why": "ci"}})
    assert demoted == [] and {s["case_id"] for s in surviving} == {"happy_path:1", "edge_cases:2"}
    assert "<success>true</success><pad>" in surviving[0]["test_body"] or \
        "largeXmlSuccess()" in surviving[0]["test_body"]
    msg = ev.build_coverage_critic_message(surviving)
    assert "Cited test:" in msg and "responseBytes" in msg

    demoted = ev.apply_coverage_verdicts(surviving, {
        "happy_path:1": {"verdict": "partial", "missing": "the success flag is at the start, not past 256"},
        "edge_cases:2": {"verdict": "partial", "missing": "the excerpt's character length is never asserted"},
    })
    assert len(demoted) == 2
    assert not plan.happy_path[1]["covered_by_unit_test"]
    assert "excerpt's character length" in plan.edge_cases[2]["unit_test_gap"]


def test_no_verdict_means_not_covered():
    surviving = [{"case_id": "happy_path:0", "case": {"covered_by_unit_test": True},
                  "test_ref": "t › n", "test_body": "{}"}]
    ev.apply_coverage_verdicts(surviving, None)
    assert surviving[0]["case"]["covered_by_unit_test"] is False


@pytest.mark.parametrize("ref", [
    "src/a.test.ts",                                   # no test name
    "src/a.test.ts › works (per PR #5 description)",    # hearsay
    "per the PR summary",
])
def test_refs_without_file_and_test_name_are_rejected(ref):
    plan = TestPlan(happy_path=[{"title": "t", "covered_by_unit_test": True, "unit_test_ref": ref,
                                 "coverage_evidence": "expect(1)"}], edge_cases=[], regression_checklist=[])
    demoted, surviving = ev.enforce_unit_test_coverage(plan, {"src/a.test.ts": 'it("works", () => { expect(1) })'},
                                                       {"src/a.test.ts": {"status": "runs", "why": ""}})
    assert surviving == [] and plan.happy_path[0]["covered_by_unit_test"] is False


def test_extracts_python_and_go_test_bodies():
    py = "def test_rounds():\n    assert r(1.005) == 1.01\n\ndef test_other():\n    pass\n"
    assert ev.extract_test_body(py, "test_rounds").strip().endswith("== 1.01")
    go = "func TestRounds(t *testing.T) {\n\tif r(1) != 1 { t.Fail() }\n}\n"
    assert "t.Fail()" in ev.extract_test_body(go, "TestRounds")


# ── B: deployment ────────────────────────────────────────────────────────────

def test_not_deployed_banner_names_the_environment_and_source():
    status = ev.build_deployment_status(INPUTS["deploy_state"], None, [], "2026-10-12 23:40 UTC")
    assert status["status"] == "not_deployed"
    assert status["warnings"][0].startswith("Not deployed to integ as of ")
    assert "main-ab5a6d3" in status["warnings"][0]


def test_a_note_saying_not_deployed_reaches_the_banner_without_a_deploy_source():
    status = ev.build_deployment_status(
        [], {"deployed": "no", "statement": "Not confirmed deployed anywhere yet", "source": f"comment {TESTING_NOTE}"},
        ["acme/x"], "now")
    assert status["status"] == "not_deployed"
    assert status["warnings"] == [f"Not confirmed deployed anywhere yet (comment {TESTING_NOTE})"]


def test_unchecked_deployment_is_said_out_loud():
    status = ev.build_deployment_status([], None, ["acme/x"], "now")
    assert status["status"] == "unknown"
    assert "not checked" in status["warnings"][0]


def test_deployed_cases_get_a_deploy_check_and_local_ones_do_not():
    plan = _v1_plan()
    needs_spec = copy.deepcopy(V1["needs_spec_cases"])
    plan.integration_tests.append(needs_spec[1])  # the "non-production deployment" case
    plan.integration_tests[-1]["step_grounding"] = [{"step": 2, "element": "x", "kind": "control", "source": "pr:a"}]
    fixed = ev.enforce_deploy_precondition(plan, INPUTS["merge_commit"], None)

    deployed = plan.integration_tests[-1]
    assert deployed["steps"][0].startswith("Deploy check: confirm this environment runs merged commit 2824789")
    assert deployed["step_grounding"][0]["step"] == 3, "step numbers follow the inserted step"
    assert not plan.happy_path[0].get("deploy_check_added"), "the offline harness needs no deployment"
    line = plan.regression_checklist[9]
    assert "Deploy check first: confirm the environment runs merged commit 2824789" in line
    assert line.startswith("🟡"), "the priority marker stays first"
    assert "regression_checklist:9" in fixed
    assert len(plan.regression_checklist) == len(V1["regression_checklist"])


def test_a_local_checklist_line_that_mentions_the_deployed_commit_gets_no_deploy_check():
    """From the first eval run: running the suite by hand needs no environment."""
    line = "🟡 Run `yarn jest __tests__/request-processor.unit.js` by hand against the deployed commit"
    plan = TestPlan(happy_path=[], edge_cases=[], regression_checklist=[line])
    ev.enforce_deploy_precondition(plan, INPUTS["merge_commit"], None)
    assert plan.regression_checklist == [line]


# ── End to end through plan_service ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_single_replaying_v1_produces_the_evidence_sections():
    """Plan 608 v1 replayed through the pipeline: the prompt carries the note
    and the repo facts, and the plan that comes out says it is not deployed,
    lists the acceptance gaps, states the CI and lockfile facts, and has no
    unproven coverage — whatever the model wrote."""
    plan = _v1_plan()
    seen = {}

    class _LLM:
        async def generate_test_plan(self, **kwargs):
            seen.update(kwargs)
            return plan

        async def verify_unit_coverage(self, surviving):
            raise AssertionError("no claim should survive to the critic")

    async def _noop(*a, **k):
        return None

    async def _deploy(*a, **k):
        return copy.deepcopy(INPUTS["deploy_state"])

    async def _repo(dev_info, mentioned):
        return await ev.gather_repo_evidence(FakeGitHub(), dev_info["pull_requests"], mentioned)

    request = GenerateTestPlanRequest(
        ticket_key="SK-2826",
        summary=INPUTS["summary"],
        description=INPUTS["description"],
        issue_type=INPUTS["issue_type"],
        development_info=_dev_info(),
        comments=_comments(),
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
        patch.object(plan_service, "_read_deploy_state", _deploy),
        patch.object(plan_service, "_read_repo_evidence", _repo),
    ):
        response = await plan_service.generate_single(request)

    # What the model was given.
    dev = seen["development_info"]
    assert [c["id"] for c in seen["comments"]] == ["336014", TESTING_NOTE]
    assert dev["repo_evidence"][0]["test_sources"][UNIT_TEST_FILE]
    assert any("explicit byte limit" in r["text"] for r in dev["requirements"])

    # (2) not deployed, and a deploy check where a deployment is assumed
    assert response["deployment_status"]["status"] == "not_deployed"
    assert "Deploy check first" in response["regression_checklist"][9]
    # (3) nothing unproven left as covered
    for section in ("happy_path", "edge_cases", "integration_tests"):
        assert not any(c.get("covered_by_unit_test") for c in response[section])
    # (4) and (6)
    kinds = {f["kind"] for f in response["build_and_ci"]}
    assert {"tests_not_run_in_ci", "lockfile_not_shipped"} <= kinds
    # (5) the model assessed nothing, so every requirement is a gap
    gaps = {g["requirement"] for g in response["acceptance_gaps"]}
    assert any("explicit byte limit" in g for g in gaps)
    assert any("regression test proves the exploit path" in g for g in gaps)
    # (1) v1 cites no note, and the plan says so
    assert [n["id"] for n in response["evidence_notes"]["unused"]] == ["336014", TESTING_NOTE]
    assert response["source_provenance"]["unused_testing_notes"] == ["336014", TESTING_NOTE]


def test_prompt_carries_the_note_the_repo_facts_and_the_requirements():
    """(1)'s precondition: the prompt shows the note with its id and marks it
    primary, and shows the facts B–F rely on."""
    import asyncio
    from src.app.llm_client import ClaudeClient

    evidence = asyncio.run(_evidence())
    reqs = ev.extract_requirements(INPUTS["description"])
    client = object.__new__(ClaudeClient)
    prompt = client._build_prompt(
        "SK-2826", INPUTS["summary"], INPUTS["description"], {},
        _dev_info(repo_evidence=evidence, requirements=reqs, deploy_state=INPUTS["deploy_state"]),
        comments=_comments(),
    )
    assert f"**Comment {TESTING_NOTE} by Dev Agent**" in prompt
    assert "PRIMARY SOURCE (testing notes)" in prompt
    assert "webhook-traffic-generator" in prompt and "set image tag main-2824789" in prompt
    assert f"TEST FILE {UNIT_TEST_FILE} — CI status: NOT_RUN" in prompt
    assert "Tests exist but are not run in CI" in prompt
    assert "does not use the committed lockfile" in prompt
    assert "#5: NOT INCLUDED" in prompt
    assert 'explicit byte limit' in prompt and "requirement_check" in prompt


def test_source_inspection_is_not_a_deployed_environment():
    """From the live regeneration: 'no deployed environment' is not one."""
    case = {"environment": "acme/repo @ 2824789f (source inspection; no deployed environment needed)",
            "steps": ["grep for .clone()"]}
    assert not ev.needs_deployed_env(case)


@pytest.mark.parametrize("cites_note", [True, False])
def test_a_step_from_a_cited_testing_note_is_labelled_not_quarantined(cites_note):
    """From the live v2/v3 regeneration: the note's trigger call was stamped
    'Unverified — assumption' and flagged for quarantine, undoing rule A."""
    from src.app.step_grounding import check_step_grounding

    case = {
        "title": "TC2", "covers_acs": ["SK-2826-AC1"], "grounded_in": ["ticket:SK-2826-desc"],
        "steps": ["Configure the receiver", "Run node index.js, then `curl -X POST localhost:8080/send` once"],
        "step_grounding": [{"step": 2, "element": "POST localhost:8080/send", "kind": "action",
                            "source": "ticket:SK-2826-desc"}],
        "expected_verified": False,
    }
    if cites_note:
        case["source_comment_ids"] = [TESTING_NOTE]
    plan = TestPlan(happy_path=[case], edge_cases=[], regression_checklist=[])
    check_step_grounding(plan, set(), notes=ev.note_texts(_comments()))
    step = plan.happy_path[0]["steps"][1]
    if cites_note:
        assert step.startswith(f"From testing note {TESTING_NOTE} ")
        assert not plan.happy_path[0].get("needs_manual_verification")
    else:
        assert step.startswith("Unverified — assumption")
        assert plan.happy_path[0]["needs_manual_verification"] is True
