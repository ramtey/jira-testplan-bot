"""Replay stored plans through the deterministic post-generation stages.

Takes plans out of the bot store, reads their PRs, repos and deployments live
from GitHub, and runs the stages that decide build checks, CI facts and what is
a runnable case — the ones that do not depend on the model:

  - deployments discovery and deploy state (src/app/build_targets.py, deploy_state.py)
  - repo evidence: CI from every source, platform, app version (evidence_checks.py)
  - per-case build checks (evidence_checks.enforce_deploy_precondition)
  - what leaves the checklist (case_disposition.route_out_of_uat)
  - empty / non-JSON error-body variants (error_variants.py)
  - CI claims a case argues from (reachability_checks.flag_ci_claims)
  - the grounding re-read: labels, flags, verified expected results and client
    tokens checked against the cited files (grounding_verification.py)

For each plan it prints what the stored plan said and what the stages say
now. The stored plan is the model's output *after* the old stages ran, so the
replay first strips what those stages added (deploy steps), exactly as
tests/fixtures/sk2719 and sk2889 were built.

    uv run python evals/replay_store_plans.py 1 2        # plan ids
    uv run python evals/replay_store_plans.py 1 --json   # full replayed body

Read-only: nothing is written to the store, GitHub or Jira. Needs the store
(MONGODB_URI) and a GitHub token. No model calls, so no cost.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.app import build_targets as bt  # noqa: E402
from src.app import case_disposition as cd  # noqa: E402
from src.app import evidence_checks as ev  # noqa: E402
from src.app.config import settings  # noqa: E402
from src.app.deploy_state import read_deploy_state  # noqa: E402
from src.app.error_variants import require_error_body_variants  # noqa: E402
from src.app import grounding_verification as gv  # noqa: E402
from src.app import source_facts as sf  # noqa: E402
from src.app.github_client import GitHubClient  # noqa: E402
from src.app.models import TestPlan  # noqa: E402
from src.app.reachability_checks import flag_ci_claims  # noqa: E402
from src.app.services import progress_key as pk  # noqa: E402

SECTIONS = ("happy_path", "edge_cases", "integration_tests", "security_negative_tests")


async def _load(plan_id: int) -> tuple[dict, dict]:
    from pymongo import MongoClient

    db = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000).get_default_database()
    plan = db.generated_plans.find_one({"_id": plan_id})
    if not plan:
        raise SystemExit(f"plan {plan_id} is not in the store")
    run = db.runs.find_one({"_id": plan["run_id"]}) or {}
    return json.loads(plan["body"]), run


async def _pull_requests(gh: GitHubClient, provenance: dict) -> list[dict]:
    out = []
    for pr in provenance.get("pull_requests") or []:
        d = await gh.fetch_pr_details(pr["url"], include_patch=True, include_comments=False)
        if d is None:
            raise SystemExit(f"could not read {pr['url']}")
        out.append({"repository": pr["repository"], "number": d.number, "url": pr["url"],
                    "status": "MERGED" if d.merged else d.state.upper(), "merged_at": d.merged_at,
                    "merge_commit_sha": d.merge_commit_sha, "head_sha": d.head_sha,
                    "files_changed": [{"filename": f.filename, "status": f.status, "patch": f.patch}
                                      for f in d.files_changed]})
    return out


def _as_model_output(body: dict) -> TestPlan:
    b = copy.deepcopy(body)
    for s in SECTIONS:
        for c in b.get(s) or []:
            steps = c.get("steps") or []
            if c.pop("deploy_check_added", None) and steps and steps[0].startswith("Deploy check:"):
                c["steps"] = steps[1:]
    return TestPlan(happy_path=b.get("happy_path") or [], edge_cases=b.get("edge_cases") or [],
                    regression_checklist=[cd_strip(l) for l in b.get("regression_checklist") or []],
                    integration_tests=b.get("integration_tests") or [],
                    security_negative_tests=b.get("security_negative_tests") or [], risks_and_gaps=[])


def cd_strip(line: str) -> str:
    import re
    return re.sub(r"(?:Deploy check first: confirm the environment runs merged commit \S+ or later\.\s*"
                  r"|Unverified — assumption \(no code shows this\):\s*)", "", line)


async def replay(plan_id: int, as_json: bool) -> None:
    body, run = await _load(plan_id)
    gh = GitHubClient()
    prs = await _pull_requests(gh, body.get("source_provenance") or run.get("source_provenance") or {})
    deployments = await bt.discover_deployments(gh, prs, configured_sources=settings.deploy_state_sources,
                                                deployments_repos=settings.deployments_repos)
    sources = {**deployments["deploy_sources"], **settings.deploy_state_sources}
    evidence = await ev.gather_repo_evidence(gh, prs, "", deployments)
    deploy_state = await read_deploy_state(gh, prs, sources)
    targets = bt.build_targets(prs, evidence, deploy_state, deployments["deployments_repos"])
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    plan = _as_model_output(body)
    out = cd.route_out_of_uat(plan, dev_infos=[{"pull_requests": prs}])
    ev.enforce_deploy_precondition(plan, targets=targets, when=when)
    variants = require_error_body_variants(plan)
    ci_flags = flag_ci_claims(plan, evidence)
    banner = ev.build_deployment_status(deploy_state, None, [t["repo"] for t in targets], when, targets=targets)
    # What the generator was shown is replayed from the diffs alone: a stored
    # plan predates the source-facts section, so its "not shown" verdicts are
    # the ones its own prompt earned. Exploration findings are not stored, so
    # a claim resting on one reads as not shown here.
    index = await sf.read_source_index(gh, prs)
    reread = await gv.run_grounding_verification(
        plan, index, gh, shown=gv.shown_lines([{"pull_requests": prs}]),
        token_sources=settings.runner_token_sources)

    new = {s: getattr(plan, s) for s in SECTIONS} | {"regression_checklist": plan.regression_checklist} | {
        k: out[k] for k in cd.OUT_OF_UAT_KEYS}
    key = body.get("ticket_key")
    print(f"\n━━ plan {plan_id} · {key} ━━")
    print(f"progress key: stored {pk.build_progress_key([key], body)} → replayed {pk.build_progress_key([key], new)}")
    print("targets:", [(t["repo"], (t["commit"] or "")[:7], t["platform"], (t.get("app") or {}).get("version"))
                       for t in targets])
    print("deployments:", json.dumps({k: deployments[k] for k in ("deployments_repos", "deploy_sources")}),
          "pipelines:", {r: [p["source"] for p in ps] for r, ps in deployments["pipelines"].items()},
          "errors:", deployments["errors"])
    print("deploy state:", [(e["env"], e["repo"], e["tag"], [p["included"] for p in e["prs"]]) for e in deploy_state])
    print("CI, stored :", [f["finding"][:140] for f in body.get("build_and_ci") or []])
    print("CI, now    :", [f"{e['repo']}: {f['kind']}: {f['finding'][:140]}" for e in evidence
                           for f in (e["build"] or {}).get("findings") or []])
    print("banner     :", banner)
    print("moved out of UAT:", out["moved"])
    print("error-body variants added:", variants, "· CI claims flagged:", ci_flags)
    print(f"grounding re-read: {reread.get('checked')} checked, {reread.get('downgraded')} downgraded, "
          f"{reread.get('unchecked')} unchecked, client token needed by {reread.get('needs_client_equivalent_token')}"
          + (f" — {reread['error']}" if reread.get("error") else ""))
    for c in reread.get("downgraded_claims") or []:
        print(f"  ↓ {c['case_id']} {c['kind']}: {c['claim'][:90]} — {c['reason'][:220]}")
    for s in SECTIONS:
        for i, c in enumerate(getattr(plan, s)):
            if c.get("deploy_check_added") or c.get("deploy_check_replaced"):
                print(f"  {s}:{i} first step: {c['steps'][0][:260]}")
                break
    if as_json:
        print(json.dumps(new, indent=1, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("plan_ids", nargs="+", type=int)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    for pid in args.plan_ids:
        asyncio.run(replay(pid, args.json))


if __name__ == "__main__":
    main()
