"""Does the code a case cites actually say what the case claims?

`expected_verified: true` tells a reviewer to stop checking. This scores
whether that was earned, against plans already on disk — so it costs nothing
to generate and can be re-run over any results directory.

Three phases, cheapest first, because most of the answer is free:

    resolve   open each cited file at the PR's merge SHA      GitHub only, $0
    judge     ask whether the lines support the assertion     ~$1-4
    score     aggregate                                       free

The verdicts are deliberately three, from one call:

    SUPPORTS    the cited lines establish the expected result   -> groundedness
    SILENT      real, relevant code that does not decide it     -> honest miss
    CONTRADICTS the code says otherwise                         -> FALSE FLAG

A false flag is the one that costs QA a day, so it comes from the same call as
groundedness and cannot drift away from it.

Two numbers are always reported together. Groundedness alone is gamed by
setting `expected_verified: false` everywhere and scoring 100% of nothing, so
the verified *rate* — how much of the plan makes a checkable claim at all —
sits beside it. A change that raises one by lowering the other is a
regression.

    python evals/score_grounding.py resolve
    python evals/score_grounding.py judge
    python evals/score_grounding.py score

Reads EVAL_RESULTS_DIR (default ~/sk_eval_results), the same directory
replay_bounces.py writes plans into.
"""

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from src.app.citation_integrity import inspect_citation
from src.app.config import settings
from src.app.http_retry import retrying_client
from src.app.model_capabilities import supports_temperature

RESULTS = Path(os.environ.get("EVAL_RESULTS_DIR", Path.home() / "sk_eval_results"))
PLANS = RESULTS / "plans"
CLAIMS = RESULTS / "grounding"
SNIPPETS = RESULTS / "snippets"

# Never the model that wrote the plans: a model grading its own citation
# credits its own reasoning. Same tier, different model.
JUDGE_MODEL = os.environ.get("EVAL_GROUNDING_JUDGE", "claude-opus-4-8")

#: Lines of context either side of the cited line handed to the judge. Wide
#: enough to contain a function, narrow enough that the judge is answering
#: about the citation rather than about the file.
CONTEXT_LINES = 40

CASE_SECTIONS = ("happy_path", "edge_cases", "integration_tests", "needs_spec_cases")

VERDICT_TOOL = {
    "name": "report_grounding",
    "description": "Report whether the cited code establishes the case's expected result.",
    # strict makes the API enforce the shape rather than trusting the model to.
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["SUPPORTS", "SILENT", "CONTRADICTS"],
                "description": (
                    "SUPPORTS: the cited lines establish this expected result — a "
                    "reader could stop checking. SILENT: the code is real and "
                    "related but does not determine the expected value; the "
                    "citation does not earn the claim. CONTRADICTS: the code "
                    "specifies different behaviour from what the case expects."
                ),
            },
            "evidence": {
                "type": "string",
                "description": (
                    "Quote the specific line(s) you based the verdict on, so a "
                    "human can check you. For CONTRADICTS, say what the code does "
                    "instead."
                ),
            },
        },
        "required": ["verdict", "evidence"],
    },
}

JUDGE_PROMPT = """You are auditing one claim in a QA test plan.

The plan asserts this expected result:
{expected}

It claims that result was read out of the implementation, and cites:
{source}

Here is that file at the commit the plan was written against, around the cited line.
Line numbers are shown. The cited line is marked with >>>.

{snippet}

Decide whether the cited code ESTABLISHES the expected result.

- SUPPORTS only when a reader of these lines could conclude the expected result
  is what the code does. The value, condition or behaviour asserted must be
  visible here, not merely plausible given the file's topic.
- SILENT when the code is real and related but does not determine the asserted
  result — imports, type declarations, a component that renders something
  without fixing the asserted value, a call to a function defined elsewhere.
  Being on the right screen is not the same as specifying the behaviour.
- CONTRADICTS when the code specifies behaviour that differs from the
  assertion — a different default, an opposite condition, a different string,
  an absent control the case says exists.

Judge only these lines. Do not credit the claim for what the rest of the
codebase might contain: the plan cited THIS location as its evidence."""


def _parse_locator(expected_source):
    """(path, start, end) from `<path>:<line>` or `<path>:<a>-<b>`, else None."""
    raw = (expected_source or "").strip()
    if not raw:
        return None
    first = raw.split()[0].rstrip(",;")
    if ":" not in first:
        return None
    path, _, lines = first.rpartition(":")
    if not path:
        return None
    start, _, end = lines.partition("-")
    try:
        s = int(start)
        e = int(end) if end else s
    except ValueError:
        return None
    return path, s, e


def iter_claims(plan):
    """Every case in a plan that promises a verified expected result."""
    for section in CASE_SECTIONS:
        for i, case in enumerate(plan.get(section) or []):
            if not isinstance(case, dict):
                continue
            if case.get("expected_verified") is not True:
                continue
            yield section, i, case


def plan_repos(plan):
    """(repository, sha) pairs the plan was grounded in, best SHA first."""
    prov = plan.get("source_provenance") or {}
    out = []
    for pr in prov.get("pull_requests") or []:
        repo, sha = pr.get("repository"), pr.get("head_sha")
        if repo and sha:
            out.append((repo, sha))
    # Deduplicate, keeping order: a ticket often has several PRs per repo.
    seen, uniq = set(), []
    for repo, sha in out:
        if (repo, sha) in seen:
            continue
        seen.add((repo, sha))
        uniq.append((repo, sha))
    return uniq


async def fetch_file(client, repo, sha, path):
    """(lines, reason). `lines` is None when the file could not be read.

    A 404 is genuine absence at that commit. Anything else — a throttle, a
    5xx, a token that cannot see the repo — is a failure to look, and must not
    be recorded as the file not existing.
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={sha}"
    headers = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    r = await client.get(url, headers=headers)
    if r.status_code == 404:
        return None, "absent"
    if r.status_code != 200:
        return None, f"unreadable: HTTP {r.status_code}"
    body = r.json()
    content = body.get("content")
    if not content:
        # Files over ~1MB come back without inline content.
        raw = await client.get(
            f"https://raw.githubusercontent.com/{repo}/{sha}/{path}", headers=headers)
        if raw.status_code != 200:
            return None, f"unreadable: HTTP {raw.status_code} on raw"
        return raw.text.splitlines(), "ok"
    try:
        text = base64.b64decode(content).decode("utf-8", errors="replace")
    except Exception as e:
        return None, f"unreadable: {type(e).__name__}"
    return text.splitlines(), "ok"


def render_snippet(lines, start, end):
    lo = max(1, start - CONTEXT_LINES)
    hi = min(len(lines), end + CONTEXT_LINES)
    out = []
    for n in range(lo, hi + 1):
        mark = ">>>" if start <= n <= end else "   "
        out.append(f"{mark} {n:5d} | {lines[n - 1]}")
    return "\n".join(out)


def claim_path(key):
    return CLAIMS / f"{key}.json"


async def phase_resolve(limit):
    CLAIMS.mkdir(parents=True, exist_ok=True)
    SNIPPETS.mkdir(parents=True, exist_ok=True)
    plans = sorted(PLANS.glob("*.json"))
    todo = [p for p in plans if not claim_path(p.stem).exists()]
    if limit:
        todo = todo[:limit]
    print(f"{len(plans)} plans | resolving {len(todo)}\n")
    if not settings.github_token:
        sys.exit("GITHUB_TOKEN is not set; every lookup would fail as unreadable.")

    async with retrying_client(timeout=30) as client:
        for i, pf in enumerate(todo, 1):
            plan = json.loads(pf.read_text())
            key = pf.stem
            if plan.get("excluded") or plan.get("error"):
                claim_path(key).write_text(json.dumps(
                    {"key": key, "skipped": plan.get("excluded") or "generation failed",
                     "claims": []}, indent=2))
                print(f"  [{i}/{len(todo)}] {key} skipped")
                continue
            repos = plan_repos(plan)
            claims, cache = [], {}
            for section, idx, case in iter_claims(plan):
                src = case.get("expected_source")
                rec = {
                    "key": key, "section": section, "index": idx,
                    "title": case.get("title"), "expected": case.get("expected"),
                    "expected_source": src,
                    "shape_problems": inspect_citation(src),
                }
                loc = _parse_locator(src)
                if not loc or not repos:
                    rec["resolution"] = "unparseable" if not loc else "no_repo"
                    claims.append(rec)
                    continue
                path, start, end = loc
                rec.update({"path": path, "line_start": start, "line_end": end})
                lines, why = None, "absent"
                for repo, sha in repos:
                    ck = (repo, sha, path)
                    if ck not in cache:
                        cache[ck] = await fetch_file(client, repo, sha, path)
                    lines, why = cache[ck]
                    if lines is not None:
                        rec["repo"], rec["sha"] = repo, sha
                        break
                    if why.startswith("unreadable"):
                        rec["repo"], rec["sha"] = repo, sha
                        break
                if lines is None:
                    # "We could not look" and "it is not there" are different
                    # facts, and only the second is evidence about the plan.
                    rec["resolution"] = "path_absent" if why == "absent" else "unreadable"
                    rec["detail"] = why
                elif start > len(lines):
                    rec["resolution"] = "line_out_of_range"
                    rec["file_lines"] = len(lines)
                else:
                    rec["resolution"] = "ok"
                    rec["file_lines"] = len(lines)
                    sid = f"{key}-{section}-{idx}"
                    (SNIPPETS / f"{sid}.txt").write_text(
                        render_snippet(lines, start, min(end, len(lines))))
                    rec["snippet_id"] = sid
                claims.append(rec)
            claim_path(key).write_text(json.dumps(
                {"key": key, "repos": repos, "claims": claims}, indent=2))
            counts = {}
            for c in claims:
                counts[c["resolution"]] = counts.get(c["resolution"], 0) + 1
            print(f"  [{i}/{len(todo)}] {key:9} {len(claims):3d} claims  {counts}")


async def judge_one(client, claim):
    snippet = (SNIPPETS / f"{claim['snippet_id']}.txt").read_text()
    body = {
        "model": JUDGE_MODEL,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": JUDGE_PROMPT.format(
            expected=claim.get("expected") or "(none given)",
            source=claim.get("expected_source"),
            snippet=snippet)}],
        "tools": [VERDICT_TOOL],
        "tool_choice": {"type": "tool", "name": "report_grounding"},
    }
    if supports_temperature(JUDGE_MODEL):
        body["temperature"] = 0.0
    r = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={"anthropic-version": "2023-06-01",
                 "x-api-key": settings.anthropic_api_key,
                 "content-type": "application/json"},
        json=body)
    r.raise_for_status()
    data = r.json()
    block = next((b for b in data["content"] if b.get("type") == "tool_use"), None)
    if block is None:
        raise RuntimeError("judge returned no tool_use block")
    verdict = block["input"].get("verdict")
    if verdict not in ("SUPPORTS", "SILENT", "CONTRADICTS"):
        raise RuntimeError(f"judge returned {verdict!r}")
    return verdict, block["input"].get("evidence", ""), data.get("usage") or {}


async def phase_judge(limit):
    files = sorted(CLAIMS.glob("*.json"))
    if not files:
        sys.exit("Nothing resolved yet. Run resolve first.")
    if JUDGE_MODEL == settings.llm_model:
        print(f"  WARNING: judge and generator are both {JUDGE_MODEL}; a model "
              f"grading its own citations is generous. Set EVAL_GROUNDING_JUDGE.\n")
    todo = []
    for f in files:
        doc = json.loads(f.read_text())
        for c in doc.get("claims") or []:
            if c.get("resolution") == "ok" and "verdict" not in c:
                todo.append((f, doc, c))
    if limit:
        todo = todo[:limit]
    print(f"judging {len(todo)} resolved claims with {JUDGE_MODEL}\n")
    # Opus-tier rates, so the printed figure is an upper bound if the judge
    # is ever pointed at something cheaper.
    tally = {"in": 0, "out": 0}
    async with httpx.AsyncClient(timeout=180.0) as client:
        done = 0
        for f, doc, claim in todo:
            try:
                verdict, evidence, usage = await judge_one(client, claim)
                claim["verdict"], claim["evidence"] = verdict, evidence
                tally["in"] += usage.get("input_tokens", 0)
                tally["out"] += usage.get("output_tokens", 0)
            except Exception as e:
                # Leave it unjudged rather than guessing. A guessed SUPPORTS
                # inflates the score; a guessed CONTRADICTS invents a bug.
                claim["judge_error"] = f"{type(e).__name__}: {e}"
                print(f"    FAILED {claim['key']} {claim['section']}:{claim['index']} "
                      f"{str(e)[:70]}")
            f.write_text(json.dumps(doc, indent=2))
            done += 1
            if done % 25 == 0:
                print(f"    {done}/{len(todo)}")
    cost = tally["in"] / 1e6 * 5.0 + tally["out"] / 1e6 * 25.0
    print(f"  judged {done} | {tally['in']:,} in + {tally['out']:,} out tokens "
          f"| ~${cost:.2f} at Opus rates")


def phase_score():
    files = sorted(CLAIMS.glob("*.json"))
    if not files:
        sys.exit("Nothing resolved yet. Run resolve first.")
    res, verd = {}, {}
    total_cases = verified_cases = 0
    contradictions, unreadable = [], 0
    for f in files:
        doc = json.loads(f.read_text())
        plan_file = PLANS / f.name
        if plan_file.exists():
            plan = json.loads(plan_file.read_text())
            if not (plan.get("excluded") or plan.get("error")):
                for section in CASE_SECTIONS:
                    for case in plan.get(section) or []:
                        if isinstance(case, dict):
                            total_cases += 1
                            if case.get("expected_verified") is True:
                                verified_cases += 1
        for c in doc.get("claims") or []:
            res[c["resolution"]] = res.get(c["resolution"], 0) + 1
            if c["resolution"] == "unreadable":
                unreadable += 1
            v = c.get("verdict")
            if v:
                verd[v] = verd.get(v, 0) + 1
                if v == "CONTRADICTS":
                    contradictions.append(c)

    judged = sum(verd.values())
    print(f"\n  JUDGE {JUDGE_MODEL}\n")
    print(f"  Cases ..................... {total_cases}")
    if total_cases:
        print(f"  Verified rate ............. {verified_cases}/{total_cases}   "
              f"{verified_cases / total_cases:.0%}   "
              f"(how much of the plan makes a checkable claim)")
    print("\n  Citations resolved against the code at the cited commit:")
    for k in ("ok", "path_absent", "line_out_of_range", "unparseable", "no_repo", "unreadable"):
        if res.get(k):
            print(f"    {k:20} {res[k]}")
    if unreadable:
        print(f"    ^ 'unreadable' is not evidence about the plan — we could not look.")

    checkable = sum(v for k, v in res.items() if k != "unreadable")
    broken = res.get("path_absent", 0) + res.get("line_out_of_range", 0)
    if checkable:
        print(f"\n  Citations that cannot be what they claim, with no LLM at all:"
              f" {broken}/{checkable}  {broken / checkable:.0%}")
    if judged:
        print(f"\n  Of {judged} citations whose code we read:")
        for k in ("SUPPORTS", "SILENT", "CONTRADICTS"):
            n = verd.get(k, 0)
            print(f"    {k:12} {n:4d}   {n / judged:.0%}")
        print(f"\n  GROUNDEDNESS ....... {verd.get('SUPPORTS', 0) / judged:.0%}"
              f"   (of read citations)")
        print(f"  FALSE-FLAG RATE .... {verd.get('CONTRADICTS', 0) / judged:.0%}"
              f"   (the ones that cost QA a day)")
    if contradictions:
        print(f"\n  Contradicted assertions ({len(contradictions)}) — act on these:")
        for c in contradictions[:10]:
            print(f"    {c['key']} [{c['section']}:{c['index']}] {str(c.get('title'))[:60]}")
            print(f"        cites {c.get('expected_source')}")
            print(f"        {str(c.get('evidence'))[:150]}")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("phase", choices=["resolve", "judge", "score"])
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    if args.phase == "resolve":
        asyncio.run(phase_resolve(args.limit))
    elif args.phase == "judge":
        asyncio.run(phase_judge(args.limit))
    else:
        phase_score()


if __name__ == "__main__":
    main()
