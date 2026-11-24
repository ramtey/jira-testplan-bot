"""Eval: does a generated plan state who sends each request, and expect the layer that answers first (rule AM)?

SK-3174 (plan 752 v1) had regression lines a tester could not run as written:
a batch request with no auth to a route whose auth hook answers 401 before the
batch check, a wrong-Host request expecting the app's 421 through a
Cloudflare-proxied hostname that answers 403 first, and a key-set outage that
only "the platform team" could cause, where a unit test asserts it.

tests/fixtures/sk3174/ holds a synthetic, sanitized version (host
mcp.example.test, org acme): one authenticated JSON-RPC route with an auth hook
before a host check and a batch check, a CDN in the deployment values, and a
unit test for each of 401 / batch / 421 / 503. This runs it through the real
generator, with GitHub served from the fixture (no GitHub calls, no quota), and
checks every case and regression line about:

  1. batch requests  — states its principal/auth
  2. wrong Host      — states its principal/auth AND names the layer that answers
                       from outside (CDN/WAF/Cloudflare/edge)
  3. key-set outage  — cites the unit test (server/test/app.test.ts) or is
                       marked manual with the owning team named

Each check also reports whether the model wrote it or the save-time re-check
(src/app/request_layers.py) fixed it, from the plan's rule AM findings.

    uv run python evals/sk3174_request_layers.py [--runs N]    ~$1 per run

Run records are stubbed, not written. Results go to $EVAL_RESULTS_DIR/sk3174
(default ~/sk_eval_results/sk3174).
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

from src.app import request_layers as rl  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sk3174"
INPUTS = json.loads((FIXTURE / "inputs.json").read_text())
REPO_DIR = FIXTURE / "repo"
REPO_FILES = {str(p.relative_to(REPO_DIR)): p.read_text() for p in REPO_DIR.rglob("*") if p.is_file()}
RESULTS = Path(os.environ.get("EVAL_RESULTS_DIR", Path.home() / "sk_eval_results")) / "sk3174"
SECTIONS = ("happy_path", "edge_cases", "integration_tests", "security_negative_tests", "needs_spec_cases")


def _patch(text: str) -> str:
    lines = text.splitlines()
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + line for line in lines)


def _pull_request() -> dict:
    pr = copy.deepcopy(INPUTS["pull_request"])
    files = [{"filename": f, "status": "added", "additions": len(REPO_FILES[f].splitlines()), "deletions": 0,
              "changes": len(REPO_FILES[f].splitlines()), "patch": _patch(REPO_FILES[f])}
             for f in INPUTS["pr_files"]]
    pr.update(files_changed=files, total_additions=sum(f["additions"] for f in files), total_deletions=0)
    return pr


def _serve_github_from_fixture():
    """Every GitHubClient call answers from the fixture or with nothing — never the network."""
    from src.app.github_client import GitHubClient

    def _match(repo: str | None) -> bool:
        return not repo or repo.split("/")[-1] == INPUTS["repository"].split("/")[-1]

    async def fetch_files_at_ref(self, repo, ref, paths, max_bytes=300_000):
        return {p: REPO_FILES[p] for p in paths if _match(repo) and p in REPO_FILES}

    async def list_tree(self, repo, ref):
        return sorted(REPO_FILES) if _match(repo) else None

    async def search_code_hits(self, query, scope, max_hits=8):
        words = [w.strip("\"'").lower() for w in query.split() if ":" not in w and len(w.strip("\"'")) > 2]
        hits = []
        for path, text in REPO_FILES.items():
            low = text.lower()
            if words and all(w in low for w in words):
                frag = next((line for line in text.splitlines() if words[0] in line.lower()), "")
                hits.append({"repo": INPUTS["repository"], "path": path, "fragments": [frag[:400]]})
        return hits[:max_hits]

    async def search_relevant_files(self, repo, query, max_files=3):
        hits = await search_code_hits(self, query, f"repo:{repo}")
        return [{"path": h["path"], "ref": "main", "content": REPO_FILES[h["path"]]} for h in hits[:max_files]]

    async def _none(self, *a, **k):
        return None

    for name in ("fetch_file_from_blob_url", "fetch_commit_from_url", "blame_line", "fetch_user_profile",
                 "fetch_pr_author_commit_email", "fetch_pr_commit_messages", "fetch_pr_details",
                 "fetch_repository_context", "commit_before", "latest_commit_for_path", "compare_commits",
                 "_fetch_file_content"):
        setattr(GitHubClient, name, _none)
    GitHubClient.fetch_files_at_ref = fetch_files_at_ref
    GitHubClient.list_tree = list_tree
    GitHubClient.search_code_hits = search_code_hits
    GitHubClient.search_relevant_files = search_relevant_files


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

    async def _empty(*_a, **_k):
        return []

    async def _no_previous(*_a, **_k):
        return None, None

    plan_service._read_deploy_state = _deploy
    plan_service._load_seed_regressions = _empty
    # SK-3174's real plans are in the store; showing one would hand over the answer.
    plan_service._load_previous_plan = _no_previous
    plan_service.settings.pr_state_recheck_before_save = False


# --- checks ------------------------------------------------------------------------------

def _items(plan: dict) -> list[tuple[str, str, str]]:
    """(where, full text, expected) for every case and regression line."""
    out = []
    for section in SECTIONS:
        for i, c in enumerate(plan.get(section) or []):
            if isinstance(c, dict):
                req = c.get("request") if isinstance(c.get("request"), dict) else {}
                text = " ".join(str(x or "") for x in (c.get("title"), c.get("preconditions"), c.get("credentials"),
                                                        " ".join(s for s in c.get("steps") or [] if isinstance(s, str)),
                                                        req.get("auth"), req.get("params"), c.get("expected"),
                                                        c.get("unit_test_ref"), c.get("how_to_verify")))
                out.append((f"{section}:{i}", text, str(c.get("expected") or "")))
    for i, line in enumerate(plan.get("regression_checklist") or []):
        if isinstance(line, str):
            out.append((f"regression_checklist:{i}", line, rl._line_expected(line)))
    for i, c in enumerate(plan.get("code_review_checks") or []):
        if isinstance(c, dict):
            text = " ".join(str(c.get(k) or "") for k in ("title", "what_to_verify", "suggested_test", "why_not_uat"))
            out.append((f"code_review_checks:{i}", text, ""))
    return out


# What an item DOES, not what it mentions: "the status is not 401, 421 or 400" is not a wrong-Host case.
_BATCH = re.compile(r"\bbatch\b|JSON array|\[\s*\{\s*\\?\"jsonrpc", re.I)
_HOST = re.compile(r"\bHost:\s*(?!mcp\.example\.test\b)[\w.-]+\.[\w-]+|\bwrong[ -]host\b|"
                   r"\b(?:unknown|foreign|unlisted|spoofed|other|evil)[ -]Host\b", re.I)
_OUTAGE = re.compile(r"\bJWKS\b|\bkey[ -]?set\b|\bsigning keys\b|identity provider[^.]{0,40}(?:down|outage|unavailable)",
                     re.I)
_UNIT_TEST = re.compile(r"\.test\.ts\b|›", re.I)
# "Only if the platform team can block JWKS" is the SK-3174 defect, not a manual mark: say manual, or "<team> only".
_MANUAL_TEAM = re.compile(r"\bmanual\b[^.]{0,80}\b(?:team|Engineering|SRE|on-?call)\b|"
                          r"\b(?:Engineering|SRE|on-?call|platform team)\s+only\b", re.I)


def _about_auth_itself(text: str) -> bool:
    return bool(re.search(r"\bno (?:bearer )?token\b|\bwithout (?:a |any )?(?:bearer )?token\b|\bno Authorization\b|"
                          r"\binvalid (?:bearer )?token\b|\bmalformed (?:bearer )?token\b|\banonymous\b", text, re.I))


def _fixed_by_code(plan: dict, where: str) -> bool:
    findings = ((plan.get("source_provenance") or {}).get("plan_recheck") or {}).get("findings") or []
    section = where.split(":")[0]
    return any(f.get("rule") == "AM" and f.get("section") == section for f in findings)


def check(plan: dict) -> list[tuple[str, bool, str]]:
    items = _items(plan)
    results = []

    def runs(where: str) -> bool:
        return not where.startswith("code_review_checks")

    # An item about authentication itself (a no-token control) is counted, but need not add a token.
    batch = [(w, t, e) for w, t, e in items if runs(w) and _BATCH.search(t)]
    bad = [w for w, t, _ in batch if not rl._AUTH_STATED_RE.search(t) and not _about_auth_itself(t)]
    results.append(("1 batch-request items state their principal/auth", bool(batch) and not bad,
                    f"{len(batch)} item(s) {[w for w, _, _ in batch]}; without auth: {bad or 'none'}; "
                    f"rule AM fixed by code: {[w for w, _, _ in batch if _fixed_by_code(plan, w)] or 'none'}"))

    # Every wrong-Host item counts: an anonymous one must still say so ("Runs as: anonymous. Auth: none").
    host = [(w, t, e) for w, t, e in items if runs(w) and _HOST.search(t)]
    no_auth = [w for w, t, _ in host if not rl._AUTH_STATED_RE.search(t)]
    # Sent to the origin (port-forward, in-cluster) = input changed to pass the edge, which rule AM allows.
    no_layer = [w for w, t, _ in host if not rl._EDGE_TERM_RE.search(t) and not rl.ORIGIN_DIRECT_RE.search(t)]
    results.append(("2 wrong-Host items state auth and name the layer that answers from outside",
                    bool(host) and not no_auth and not no_layer,
                    f"{len(host)} item(s) {[w for w, _, _ in host]}; without auth: {no_auth or 'none'}; "
                    f"no CDN/WAF/edge named: {no_layer or 'none'}"))

    # A code-review entry is already "verify by unit test" (it proposes one); only runnable items are held to this.
    outage = [(w, t, e) for w, t, e in items if runs(w) and _OUTAGE.search(t)]
    weak = [w for w, t, _ in outage if not (_UNIT_TEST.search(t) or _MANUAL_TEAM.search(t))]
    # No runnable outage item is fine when the outage went to code review (a unit test to run, rule L/AM).
    reviewed = [w for w, t, _ in items if not runs(w) and _OUTAGE.search(t) and _UNIT_TEST.search(t)]
    results.append(("3 key-set outage items cite the unit test or are manual with the owning team",
                    (bool(outage) or bool(reviewed)) and not weak,
                    f"{len(outage)} runnable item(s) {[w for w, _, _ in outage]}; neither: {weak or 'none'}; "
                    f"in code review with a test: {reviewed or 'none'}"))
    return results


async def run_plan(runs: int) -> int:
    from src.app.models import GenerateTestPlanRequest
    from src.app.services import plan_service

    _serve_github_from_fixture()
    _stub_persistence()
    RESULTS.mkdir(parents=True, exist_ok=True)
    failures = 0
    for n in range(1, runs + 1):
        request = GenerateTestPlanRequest(
            ticket_key=INPUTS["ticket_key"], summary=INPUTS["summary"], description=INPUTS["description"],
            issue_type=INPUTS["issue_type"],
            development_info={"pull_requests": [_pull_request()],
                              "commits": [{"message": INPUTS["pull_request"]["title"], "author": "dev-agent",
                                           "date": INPUTS["pull_request"]["merged_at"],
                                           "url": INPUTS["pull_request"]["url"]}]},
            comments=[],
        )
        plan = await plan_service.generate_single(request)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        out = RESULTS / f"plan-{stamp}.json"
        out.write_text(json.dumps(plan, indent=1, default=str))
        print(f"\n── run {n}/{runs} → {out}")
        for name, ok, detail in check(plan):
            failures += not ok
            print(f"  {'PASS' if ok else 'FAIL'}  {name}\n        {detail}")
    print(f"\n{failures} failed check(s) across {runs} run(s)")
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--check", type=Path, help="grade a saved plan JSON instead of generating")
    args = ap.parse_args()
    if args.check:
        plan = json.loads(args.check.read_text())
        bad = 0
        for name, ok, detail in check(plan):
            bad += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}")
        sys.exit(1 if bad else 0)
    sys.exit(1 if asyncio.run(run_plan(args.runs)) else 0)


if __name__ == "__main__":
    main()
