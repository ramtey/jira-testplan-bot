"""Eval: does the generator follow evidence rules A–F on SK-2826?

SK-2826's first plan (plan 608, v1) ignored the ticket's testing notes, sent
testers to an environment that did not run the fix, called cases covered on the
strength of PR descriptions, assumed CI ran a suite nothing runs, buried two
unmet requirements, and claimed the lockfile is what ships. The inputs it was
written from are in tests/fixtures/sk2826/ (sanitized). This runs them through
the real generator and checks the six properties the fix has to hold:

  1. cites comment 336173 and uses its concrete steps
  2. a "not deployed" warning, and a deploy check before deployed cases
  3. the past-256-character XML case and the multibyte case are not
     `covered_by_unit_test`
  4. says the suite is not run in CI
  5. the missing exploit/control test and byte limit are Acceptance gaps
  6. says the Dockerfile ignores the lockfile

4 and 6 are written by code from the repo, so they hold whatever the model
does; 1, 2 (the warning's source), 3 and 5 depend on the model and the coverage
critic, which is why this is an eval and not only a unit test
(tests/test_evidence_checks.py pins the deterministic half).

    uv run python evals/sk2826_evidence.py plan [--runs N]   ~$1 per run
    uv run python evals/sk2826_evidence.py critic            ~$0.05

`critic` asks the coverage critic alone about the two v1 cases, with a control
case that the test file DOES fully cover, so a critic that calls everything
"partial" fails too.

Run records are stubbed, not written. Results go to $EVAL_RESULTS_DIR/sk2826
(default ~/sk_eval_results/sk2826).
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.app import evidence_checks as ev  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sk2826"
INPUTS = json.loads((FIXTURE / "inputs.json").read_text())
V1 = json.loads((FIXTURE / "plan608_v1.json").read_text())
RESULTS = Path(os.environ.get("EVAL_RESULTS_DIR", Path.home() / "sk_eval_results")) / "sk2826"
NOTE = "336173"
UNIT_TEST_FILE = "__tests__/request-processor.unit.js"

# Concrete things only the testing note says. A case built from the note
# carries several of them; a case written from the description carries none.
NOTE_TOKENS = {
    "trigger tool": r"webhook-traffic-generator",
    "trigger call": r"localhost:8080/send",
    "Loki query": r"acmeclose-webhook-delivery",
    "receiver": r"webhook\.site|ngrok",
    "S3 history/dlq": r"history/|dlq/",
    "deployed tag check": r"main-2824789|webhook-delivery:main",
    "rollback tag": r"main-ab5a6d3",
}


class FixtureGitHub:
    async def list_tree(self, repo, ref):
        return list(INPUTS["repo_tree"])

    async def fetch_files_at_ref(self, repo, ref, paths, max_bytes=300_000):
        return {p: INPUTS["repo_files"][p] for p in paths if p in INPUTS["repo_files"]}


def _stub_persistence():
    from src.app.services import plan_service, run_tracker

    async def _start(**_kw):
        return run_tracker.RunContext(run_id=None)

    async def _noop(*_a, **_kw):
        return None

    plan_service.run_tracker.start_run = _start
    plan_service.run_tracker.complete_with_plan = _noop
    plan_service.run_tracker.fail = _noop
    plan_service.run_tracker.skip = _noop

    async def _deploy(*_a, **_k):
        return copy.deepcopy(INPUTS["deploy_state"])

    async def _repo(dev_info, mentioned):
        return await ev.gather_repo_evidence(FixtureGitHub(), dev_info["pull_requests"], mentioned)

    async def _no_seeds(*_a, **_k):
        return []

    plan_service._read_deploy_state = _deploy
    plan_service._read_repo_evidence = _repo
    plan_service._load_seed_regressions = _no_seeds

    async def _no_previous(*_a, **_k):
        return None, None

    # Plan 608 is in Atlas under this ticket key; showing it to the generator
    # would hand the plan under test the answer it is scored against.
    plan_service._load_previous_plan = _no_previous


def _cases(plan: dict) -> list[tuple[str, int, dict]]:
    out = []
    for section in ("happy_path", "edge_cases", "integration_tests", "security_negative_tests",
                    "needs_spec_cases"):
        for i, c in enumerate(plan.get(section) or []):
            if isinstance(c, dict):
                out.append((section, i, c))
    return out


def _text(c: dict) -> str:
    return json.dumps({k: c.get(k) for k in ("title", "preconditions", "steps", "expected",
                                             "test_data", "environment")})


def check(plan: dict) -> list[tuple[str, bool, str]]:
    results = []
    cases = _cases(plan)

    # 1. cites the note and uses its steps
    citing = [c for _, _, c in cases if NOTE in (c.get("source_comment_ids") or [])]
    blob = " ".join(_text(c) for c in citing)
    hits = [name for name, rx in NOTE_TOKENS.items() if re.search(rx, blob)]
    ok = bool(citing) and len(hits) >= 5
    results.append(("1 cites comment 336173 with its concrete steps", ok,
                     f"{len(citing)} case(s) cite it; note details used: {', '.join(hits) or 'none'}"))

    # 2. not-deployed warning + deploy check first in every deployed case
    ds = plan.get("deployment_status") or {}
    deployed_cases = [(s, i, c) for s, i, c in cases if ev.needs_deployed_env(c)]
    missing = [f"{s}:{i}" for s, i, c in deployed_cases if not ev.has_deploy_check_first(c)]
    ok = ds.get("status") == "not_deployed" and any("integ" in w for w in ds.get("warnings") or []) \
        and bool(deployed_cases) and not missing
    results.append(("2 not-deployed warning and deploy-check precondition", ok,
                     f"status={ds.get('status')}; {len(deployed_cases)} deployed case(s), "
                     f"without a deploy check first: {missing or 'none'}; warnings={ds.get('warnings')}"))

    # 3. the two v1 false positives are not covered
    def is_xml_past_256(c):
        t = _text(c).lower()
        return "xml" in t and "256" in t and re.search(r"beyond|past|after|later than", t)

    def is_multibyte(c):
        return re.search(r"multi-?byte|non-ascii|utf-?8", _text(c).lower())

    bad, seen = [], []
    for s, i, c in cases:
        for label, pred in (("xml>256", is_xml_past_256), ("multibyte", is_multibyte)):
            if pred(c):
                seen.append(f"{label}@{s}:{i}")
                if c.get("covered_by_unit_test"):
                    bad.append(f"{label}@{s}:{i}")
    results.append(("3 XML-past-256 and multibyte cases not covered_by_unit_test", not bad,
                     f"found {seen or 'neither case'}; covered: {bad or 'none'}"))

    # 4 and 6. build/CI facts
    kinds = {f.get("kind"): f.get("finding") for f in plan.get("build_and_ci") or []}
    ok = (kinds.get("tests_not_run_in_ci") or "").startswith("Tests exist but are not run in CI")
    results.append(("4 says the suite is not run in CI", ok, (kinds.get("tests_not_run_in_ci") or "absent")[:160]))
    results.append(("6 flags the Dockerfile ignoring the lockfile", "lockfile_not_shipped" in kinds,
                    (kinds.get("lockfile_not_shipped") or "absent")[:160]))

    # 5. the two unmet requirements are Acceptance gaps
    gaps = plan.get("acceptance_gaps") or []
    byte = next((g for g in gaps if "explicit byte limit" in g["requirement"]), None)
    test = next((g for g in gaps if "regression test proves the exploit path" in g["requirement"]), None)
    ok = bool(byte and test)
    results.append(("5 byte limit and exploit/control test are Acceptance gaps", ok,
                    f"byte limit: {byte and byte['status']}; exploit test: {test and test['status']} "
                    f"(model-assessed: {bool(byte and test and 'not_assessed' not in (byte['status'], test['status']))})"))
    return results


async def run_plan(runs: int) -> int:
    from src.app.models import GenerateTestPlanRequest
    from src.app.services import plan_service

    _stub_persistence()
    RESULTS.mkdir(parents=True, exist_ok=True)
    failures = 0
    for n in range(1, runs + 1):
        request = GenerateTestPlanRequest(
            ticket_key="SK-2826",
            summary=INPUTS["summary"],
            description=INPUTS["description"],
            issue_type=INPUTS["issue_type"],
            development_info={"pull_requests": copy.deepcopy(INPUTS["pull_requests"]),
                              "commits": INPUTS["commits"]},
            comments=[c for c in INPUTS["comments"] if c["id"] != "336465"],
        )
        plan = await plan_service.generate_single(request)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        (RESULTS / f"plan-{stamp}.json").write_text(json.dumps(plan, indent=1, default=str))
        print(f"\n── run {n}/{runs} → {RESULTS / f'plan-{stamp}.json'}")
        for name, ok, detail in check(plan):
            failures += not ok
            print(f"  {'PASS' if ok else 'FAIL'}  {name}\n        {detail}")
    print(f"\n{failures} failed check(s) across {runs} run(s)")
    return failures


# Case → the test that cites it, and whether that test fully proves it.
CRITIC_ITEMS = [
    ("happy_path:1", "reads a large SOFTPRO_LEGACY_XML success response directly without cloning",
     V1["happy_path"][1], "partial|none"),
    ("edge_cases:2", "logs the full byte length of the body alongside the excerpt",
     V1["edge_cases"][2], "partial|none"),
    ("control:0", "logs a short body unchanged",
     {"title": "A short 2xx body is logged verbatim with its byte count",
      "steps": ["Have the receiver return HTTP 200 with the body \"ok\".",
                "Call processPayload with a JSON payload and capture the logger.info entry."],
      "expected": "`resp` is exactly \"ok\" and `responseBytes` is 2."},
     "full"),
]


async def run_critic() -> int:
    from src.app.llm_client import get_llm_client

    content = INPUTS["repo_files"][UNIT_TEST_FILE]
    surviving = [{"case_id": cid, "case": case, "test_ref": f"{UNIT_TEST_FILE} › {name}",
                  "test_body": ev.extract_test_body(content, name)}
                 for cid, name, case, _ in CRITIC_ITEMS]
    verdicts = await get_llm_client().verify_unit_coverage(surviving)
    if verdicts is None:
        print("FAIL  the coverage critic did not run")
        return 1
    failures = 0
    for cid, name, _, want in CRITIC_ITEMS:
        v = verdicts.get(cid) or {}
        ok = (v.get("verdict") or "") in want.split("|")
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {cid} › {name}: {v.get('verdict')} (want {want})"
              f"\n        {v.get('missing') or ''}")
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["plan", "critic"])
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()
    failures = asyncio.run(run_plan(args.runs) if args.mode == "plan" else run_critic())
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
