"""Replay eval: would the plan we write today have caught the bug QA found?

Ground truth comes from tickets QA actually bounced. For each one we know what
the tester found, because they wrote it in the comment that sent the ticket
back. So we regenerate the plan from the ticket and ask whether any case in it
would have pointed a tester at that same problem.

Three phases, run separately because only the middle one is expensive:

    replay   generate a plan per ticket          ~$0.50-1.00/ticket, resumable
    grade    judge each plan against the bounce  ~$0.02/ticket
    score    aggregate                           free

No ticket data lives in this file or this repo. The corpus is a TSV produced by
the triage tool, passed in by path, and is expected to sit outside the repo.

    python evals/replay_bounces.py replay ~/Downloads/sk_bounce_decisions.tsv
    python evals/replay_bounces.py grade  ~/Downloads/sk_bounce_decisions.tsv
    python evals/replay_bounces.py score  ~/Downloads/sk_bounce_decisions.tsv

Point MONGODB_DB at a scratch database before replaying — each generation
writes a run record, and 37 of them do not belong in the production database.
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from src.app.config import settings
from src.app.model_capabilities import supports_temperature

RESULTS = Path(os.environ.get("EVAL_RESULTS_DIR", Path.home() / "sk_eval_results"))

# How long before the complaint an attachment is still treated as evidence
# for it. Generous on purpose: losing a legitimate screenshot only removes
# context, while keeping the failure screenshot hands over the answer.
ATTACHMENT_GRACE = timedelta(minutes=30)

# The judge is deliberately NOT the model that writes the plans: a model
# grading its own output tends to credit its own phrasing. Same capability
# tier, different model, and it still accepts forced tool use — which the
# grading call depends on.
JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "claude-opus-4-8")

GRADE_TOOL = {
    "name": "report_coverage",
    "description": "Report, per reported problem, whether the test plan would have caught it.",
    # strict makes the API enforce this schema instead of trusting the model to
    # follow it. Without it the judge occasionally returned `problems` as a list
    # of bare strings and grading died on p["covered"].
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "problems": {
                "type": "array",
                "description": (
                    "One entry per DISTINCT problem the tester reported. A comment "
                    "listing four complaints yields four entries."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "problem": {
                            "type": "string",
                            "description": "The problem, in one sentence, in the tester's own terms.",
                        },
                        "covered": {
                            "type": "boolean",
                            "description": (
                                "True ONLY if a specific case would have put a tester on this "
                                "exact behaviour. Visiting the same screen is not coverage."
                            ),
                        },
                        "evidence": {
                            "type": "string",
                            "description": (
                                "When covered: the case id and the words in it that check this "
                                "behaviour. When not covered: the nearest case and what it misses."
                            ),
                        },
                    },
                    "required": ["problem", "covered", "evidence"],
                },
            }
        },
        "required": ["problems"],
    },
}

JUDGE_PROMPT = """You are auditing whether a QA test plan would have caught a bug that a real tester found.

THE BUG THE TESTER FOUND (this is ground truth — the tester wrote this when sending the ticket back):
{reason}

THE TEST PLAN THAT WAS GENERATED FOR THIS TICKET:
{plan}

Split the tester's report into each DISTINCT problem it raises, then judge each one separately.

A problem counts as COVERED only when a specific case would have walked a tester
into that exact behaviour and given them a reason to call it wrong. Apply this strictly:

- Same screen is NOT coverage. "Open the Estimate screen and verify it renders"
  does not cover "the Down Payment amount is cut off on the Estimate screen".
- A case must check the behaviour that broke, not merely reach the place it broke.
- An assertion vague enough to pass whether or not the bug is present is NOT coverage.
- Judge only what the plan says. Do not credit it for what a thorough tester
  might have noticed anyway.

Quote the case id and its wording as evidence for every verdict, so a human can check you."""


def load_corpus(path, times_path=None):
    """Rows the human marked 'keep' — bounces a plan could plausibly have caught.

    `times_path` is the mined corpus JSON; it carries the full bounce
    timestamp, where the TSV only keeps the date. Without it the rewind cuts
    at the start of the bounce day, which is safe but throws away context.
    """
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    keep = [r for r in rows if r.get("decision") == "keep"]
    if not keep:
        sys.exit(f"No rows marked 'keep' in {path}. Nothing to replay.")

    if times_path:
        corpus = json.loads(Path(times_path).read_text())
        at = {}
        for r in corpus.get("rows", corpus):
            for b in r.get("bounces") or []:
                if (b.get("reason") or "").strip():
                    at.setdefault(r["key"], b.get("at"))
                    break
        hit = 0
        for row in keep:
            if at.get(row["key"]):
                row["bounce_at"] = at[row["key"]]
                hit += 1
        print(f"  exact bounce times for {hit}/{len(keep)} tickets")
    return keep


def _iso(ts):
    """Jira/GitHub ISO-8601 -> comparable datetime, or None if unparseable."""
    from datetime import datetime
    if not ts:
        return None
    t = str(ts).strip()
    if len(t) >= 5 and t[-5] in "+-" and t[-3] != ":":
        t = t[:-2] + ":" + t[-2:]
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _normalize(t):
    return " ".join((t or "").split()).lower()


def reason_cutoff(payload, transition_ts, reason):
    """The moment the answer first appeared on the ticket.

    NOT the status transition: the tester writes the complaint and *then*
    moves the ticket, so cutting at the transition leaves the complaint in
    the prompt. On SK-1431 the gap was three minutes, and the plan came back
    quoting the tester almost verbatim.

    Prefer the timestamp of the comment the reason was taken from. Fall back
    to the transition minus the same 6h window _find_bounce_reason searches,
    so an unmatched reason still cannot leak.
    """
    from datetime import timedelta

    want = _normalize(reason)[:60]
    best = None
    if want:
        for c in payload.get("comments") or []:
            body = _normalize(c.get("body"))
            ts = _iso(c.get("created"))
            if not ts or not body:
                continue
            if want in body or body[:60] == want:
                if best is None or ts < best:
                    best = ts
    if best is not None:
        return best
    return transition_ts - timedelta(hours=6)


def rewind(serialized, cutoff):
    """Strip everything the ticket learned at or after `cutoff`.

    Without this the eval is circular. The bounce comment is still on the
    ticket today, and the generator is fed both the comments AND the bounce
    history — so it writes cases titled "REGRESSION (bounce 1): ..." straight
    from the tester's complaint and scores a perfect 100%. We are not asking
    "would the plan have caught this?" unless the plan is written without the
    answer in front of it.

    Four things carry the answer backwards in time:
      - comments posted at/after the bounce (the complaint itself, and replies)
      - bounce_history (the complaint, rendered into the prompt verbatim)
      - PRs merged after the bounce (the fix, which describes what was wrong)
      - attachments added at/after the bounce — the tester's failure screenshot
        goes to the model as an image, and a picture of the bug is the answer

    Runs on the serialized issue, before prompt_payload() narrows attachments
    to bare URLs and throws their dates away.

    Returns (serialized, what_was_removed) so a run can be audited.
    """
    payload = serialized
    removed = {"comments": 0, "bounces": 0, "prs": 0, "attachments": 0}

    comments = payload.get("comments") or []
    # An undateable comment is dropped, not kept: we cannot prove it predates
    # the bounce, and a false keep silently leaks the answer.
    kept = [c for c in comments
            if (_iso(c.get("created")) or cutoff) < cutoff]
    removed["comments"] = len(comments) - len(kept)
    payload["comments"] = kept or None

    bounces = payload.get("bounce_history") or []
    kept_b = [b for b in bounces
              if (_iso(b.get("timestamp")) or cutoff) < cutoff]
    removed["bounces"] = len(bounces) - len(kept_b)
    payload["bounce_history"] = kept_b or None

    dev = payload.get("development_info")
    if isinstance(dev, dict):
        prs = dev.get("pull_requests") or []
        kept_pr = []
        for pr in prs:
            merged = _iso(pr.get("merged_at"))
            # An unmerged PR has no date to judge; keep it, since it cannot be
            # the fix that closed this bounce.
            if merged is None or merged < cutoff:
                kept_pr.append(pr)
        removed["prs"] = len(prs) - len(kept_pr)
        if removed["prs"]:
            dev = dict(dev)
            dev["pull_requests"] = kept_pr
            payload["development_info"] = dev

    # A tester uploads the screenshot and *then* writes the complaint — on
    # SK-1431 the gap was half a second. Anything attached in the minutes
    # before the complaint is part of the complaint, so attachments get a
    # grace window that comments do not.
    atts = payload.get("attachments") or []
    att_cutoff = cutoff - ATTACHMENT_GRACE
    kept_a = [a for a in atts if (_iso(a.get("created")) or att_cutoff) < att_cutoff]
    removed["attachments"] = len(atts) - len(kept_a)
    payload["attachments"] = kept_a or None

    return payload, removed


def suppress_run_writes():
    """Read the real database, write nothing to it.

    A scratch database sounds like the careful choice and is the wrong one.
    Seed regressions come from the team's own Bug Lens history on sibling
    tickets, so an empty — or unauthorized — database returns none of them,
    and _load_seed_regressions swallows the failure and returns []. The eval
    would then be scoring plans systematically weaker than the ones production
    writes, and reading the gap as a quality problem.

    So generation reads production, and the run records it would write are
    stubbed instead. run_id=None is already the module's documented
    "persistence unavailable" path.
    """
    from src.app.services import plan_service, run_tracker

    async def _start(**_kw):
        return run_tracker.RunContext(run_id=None)

    async def _noop(*_a, **_kw):
        return None

    plan_service.run_tracker.start_run = _start
    plan_service.run_tracker.complete_with_plan = _noop
    plan_service.run_tracker.complete = _noop
    plan_service.run_tracker.complete_with_bug_analysis = _noop
    plan_service.run_tracker.fail = _noop


def plan_path(key):
    return RESULTS / "plans" / f"{key}.json"


def grade_path(key):
    return RESULTS / "grades" / f"{key}.json"


def bounce_cutoff(row):
    """When the tester bounced it — everything from here on is the answer."""
    ts = _iso(row.get("bounce_at")) or _iso(row.get("date"))
    if ts is None:
        return None
    # Fall back to the start of the bounce day when only a date is recorded.
    # Cutting early can only remove context, never leak the answer.
    return ts


async def phase_replay(rows, limit):
    from src.app.jira_client import JiraClient
    from src.app.models import GenerateTestPlanRequest
    from src.app.services.plan_service import (
        generate_single, prompt_payload, serialize_issue,
    )
    jira = JiraClient()

    suppress_run_writes()
    (RESULTS / "plans").mkdir(parents=True, exist_ok=True)
    todo = [r for r in rows if not plan_path(r["key"]).exists()]
    done = len(rows) - len(todo)          # count before --limit, or the line lies
    if limit:
        todo = todo[:limit]
    print(f"{len(rows)} keep tickets | {done} already generated "
          f"| generating {len(todo)}\n")

    for i, row in enumerate(todo, 1):
        key = row["key"]
        cutoff = bounce_cutoff(row)
        if cutoff is None:
            print(f"  [{i}/{len(todo)}] {key} SKIPPED — no bounce timestamp to rewind to")
            continue
        print(f"  [{i}/{len(todo)}] {key} ...", end=" ", flush=True)
        try:
            serialized = serialize_issue(await jira.get_issue(key))
            cutoff = reason_cutoff(serialized, cutoff, row.get("reason"))
            prs_before = len(((serialized.get("development_info") or {})
                              .get("pull_requests")) or [])
            serialized, removed = rewind(serialized, cutoff)

            # Every PR on the ticket postdates the bounce, so the rewind left
            # no implementation to ground cases in. The bot would score near
            # zero for a reason that has nothing to do with plan quality, so
            # this is an exclusion, not a result. 3 of SK's 37 land here.
            if prs_before and removed["prs"] == prs_before:
                plan_path(key).write_text(json.dumps({
                    "excluded": "no implementation existed at bounce time — "
                                f"all {prs_before} PRs merged after the cutoff",
                    "_rewound_to": cutoff.isoformat(),
                }, indent=2))
                print(f"excluded (all {prs_before} PRs postdate the bounce)")
                continue
            plan = await generate_single(
                GenerateTestPlanRequest(**prompt_payload(serialized)))
            plan["_rewound_to"] = cutoff.isoformat()
            plan["_removed"] = removed
            # Whether design context actually reached this generation. An
            # expired Figma token silently strips it, so a run has to record
            # what it had rather than what it was configured to have.
            plan["_had_figma"] = bool(
                ((serialized.get("development_info") or {}).get("figma_context"))
            )
            plan_path(key).write_text(json.dumps(plan, indent=2, default=str))
            n = len(plan.get("happy_path") or []) + len(plan.get("edge_cases") or [])
            print(f"ok ({n} cases; hid {removed['comments']} comments, "
                  f"{removed['bounces']} bounces, {removed['prs']} PRs, "
                  f"{removed['attachments']} images)")
        except Exception as e:
            # Record the failure. A ticket we could not generate for is not a
            # ticket the plan missed — scoring must be able to tell them apart.
            plan_path(key).write_text(json.dumps(
                {"error": f"{type(e).__name__}: {e}"}, indent=2))
            print(f"FAILED {type(e).__name__}: {str(e)[:90]}")


def render_plan(plan):
    """The plan as the judge sees it: every case, with a stable id."""
    lines = []
    # needs_spec_cases belongs here too. They are real cases a tester would
    # run, just flagged as needing an AC to confirm the expected value — and
    # on a ticket whose ACs are thin, most of the plan lands there. Leaving
    # the section out hid the bulk of the plan from the judge and scored it
    # as a miss.
    for section in ("happy_path", "edge_cases", "integration_tests", "needs_spec_cases"):
        for i, case in enumerate(plan.get(section) or []):
            if not isinstance(case, dict):
                continue
            bits = [f"[{section}:{i}] {case.get('title') or case.get('scenario') or ''}"]
            for field in ("steps", "expected", "expected_result", "test_data", "why"):
                v = case.get(field)
                if not v:
                    continue
                v = " ".join(v) if isinstance(v, list) else str(v)
                bits.append(f"    {field}: {v}")
            lines.append("\n".join(bits))
    for i, item in enumerate(plan.get("regression_checklist") or []):
        lines.append(f"[regression:{i}] {item}")
    return "\n".join(lines) or "(the plan contained no cases)"


async def judge(client, reason, plan):
    body = {
        "model": JUDGE_MODEL,
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": JUDGE_PROMPT.format(
            reason=reason, plan=render_plan(plan))}],
        "tools": [GRADE_TOOL],
        "tool_choice": {"type": "tool", "name": "report_coverage"},
    }
    if supports_temperature(JUDGE_MODEL):
        body["temperature"] = 0.0
    r = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={"anthropic-version": "2023-06-01",
                 "x-api-key": settings.anthropic_api_key,
                 "content-type": "application/json"},
        json=body,
    )
    r.raise_for_status()
    data = r.json()
    block = next((b for b in data["content"] if b.get("type") == "tool_use"), None)
    if block is None:
        raise RuntimeError("judge returned no tool_use block")
    problems = block["input"].get("problems")
    if not isinstance(problems, list):
        raise RuntimeError(f"judge returned {type(problems).__name__}, not a list")
    # A malformed entry is dropped, never coerced. Guessing that an unreadable
    # verdict meant "covered" would inflate the score; guessing "missed" would
    # invent a failure. Neither belongs in a number someone acts on.
    clean = [p for p in problems
             if isinstance(p, dict) and isinstance(p.get("covered"), bool)]
    if len(clean) != len(problems):
        logging.warning("judge returned %d unusable verdicts for this ticket",
                        len(problems) - len(clean))
    if not clean:
        raise RuntimeError("judge returned no usable verdicts")
    return clean


async def phase_grade(rows, limit):
    (RESULTS / "grades").mkdir(parents=True, exist_ok=True)
    todo = [r for r in rows
            if plan_path(r["key"]).exists() and not grade_path(r["key"]).exists()]
    if limit:
        todo = todo[:limit]
    if JUDGE_MODEL == settings.llm_model:
        print(f"  WARNING: judge and generator are both {JUDGE_MODEL}. A model "
              f"grading its own plans scores them generously — set "
              f"EVAL_JUDGE_MODEL to something else.\n")
    print(f"grading {len(todo)} plans with {JUDGE_MODEL}\n")

    async with httpx.AsyncClient(timeout=180.0) as client:
        for i, row in enumerate(todo, 1):
            key = row["key"]
            plan = json.loads(plan_path(key).read_text())
            if plan.get("excluded"):
                print(f"  [{i}/{len(todo)}] {key} skipped — {plan['excluded']}")
                continue
            if plan.get("error"):
                print(f"  [{i}/{len(todo)}] {key} skipped — generation failed")
                continue
            print(f"  [{i}/{len(todo)}] {key} ...", end=" ", flush=True)
            try:
                problems = await judge(client, row["reason"], plan)
                grade_path(key).write_text(json.dumps(
                    {"key": key, "reason": row["reason"], "problems": problems}, indent=2))
                hit = sum(1 for p in problems if p["covered"])
                print(f"{hit}/{len(problems)} caught")
            except Exception as e:
                print(f"FAILED {type(e).__name__}: {str(e)[:90]}")


def phase_score(rows):
    graded, failed, ungraded, excluded = [], [], [], []
    for row in rows:
        key = row["key"]
        if not plan_path(key).exists():
            ungraded.append(key)
        elif json.loads(plan_path(key).read_text()).get("excluded"):
            excluded.append(key)
        elif json.loads(plan_path(key).read_text()).get("error"):
            failed.append(key)
        elif grade_path(key).exists():
            g = json.loads(grade_path(key).read_text())
            # Refuse to score a grade file we cannot read cleanly. Silently
            # skipping malformed verdicts would quietly shrink the
            # denominator and report a number that looks fine.
            if any(not (isinstance(p, dict) and isinstance(p.get("covered"), bool))
                   for p in (g.get("problems") or [])):
                sys.exit(f"{key} has malformed verdicts. Delete "
                         f"{grade_path(key)} and re-run grade.")
            graded.append(g)
        else:
            ungraded.append(key)

    if not graded:
        sys.exit("Nothing graded yet. Run replay, then grade.")

    problems = [p for g in graded for p in g["problems"]]
    caught = sum(1 for p in problems if p["covered"])
    full = sum(1 for g in graded if g["problems"] and all(p["covered"] for p in g["problems"]))

    print(f"""
  MODEL     {settings.llm_model}     JUDGE  {JUDGE_MODEL}

  Problems caught ........ {caught}/{len(problems)}   {caught / len(problems):.0%}
  Bounces fully prevented  {full}/{len(graded)}   {full / len(graded):.0%}

  graded ................. {len(graded)} of {len(rows)} keep tickets
  excluded ............... {len(excluded)}   {excluded if excluded else ''}
        (no implementation existed at bounce time — not a plan-quality miss)
  generation failed ...... {len(failed)}   {failed if failed else ''}
  not yet run ............ {len(ungraded)}
""")
    # The misses are the point: this is the list you act on.
    missed = [g for g in graded if any(not p["covered"] for p in g["problems"])]
    missed.sort(key=lambda g: -sum(1 for p in g["problems"] if not p["covered"]))
    print(f"  Biggest misses ({len(missed)} bounces had at least one):")
    for g in missed[:8]:
        miss = [p for p in g["problems"] if not p["covered"]]
        print(f"    {g['key']}  missed {len(miss)} of {len(g['problems'])}")
        for p in miss[:2]:
            print(f"        - {p['problem'][:96]}")
    print()


def _score_dir(rows, results_dir):
    """(caught, total, per-ticket map) for one results directory."""
    per = {}
    caught = total = 0
    for row in rows:
        g = results_dir / "grades" / f"{row['key']}.json"
        if not g.exists():
            continue
        problems = json.loads(g.read_text())["problems"]
        if not problems:
            continue
        hit = sum(1 for p in problems if p["covered"])
        per[row["key"]] = (hit, len(problems))
        caught += hit
        total += len(problems)
    return caught, total, per


def phase_compare(rows, dir_a, dir_b):
    """Did the change help? Same corpus, same judge, one variable."""
    a, b = Path(dir_a).expanduser(), Path(dir_b).expanduser()
    ca, ta, pa = _score_dir(rows, a)
    cb, tb, pb = _score_dir(rows, b)
    if not ta or not tb:
        sys.exit(f"Nothing graded in {a if not ta else b}")

    shared = sorted(set(pa) & set(pb))
    print(f"""
  A  {a.name:28} {ca}/{ta}  {ca / ta:.0%}
  B  {b.name:28} {cb}/{tb}  {cb / tb:.0%}

  Tickets graded in both: {len(shared)}""")
    if len(shared) < len(pa) or len(shared) < len(pb):
        print(f"  (A has {len(pa)}, B has {len(pb)} — only the shared set is comparable)")

    sa, sb = sum(pa[k][0] for k in shared), sum(pb[k][0] for k in shared)
    ta, tb = sum(pa[k][1] for k in shared), sum(pb[k][1] for k in shared)
    print(f"  On the shared set: {sa}/{ta} ({sa / ta:.0%})  ->  {sb}/{tb} ({sb / tb:.0%})")

    # The judge decides how many distinct problems a QA comment describes, and
    # it does not always decide the same way. A ticket whose problem count
    # moved is not a like-for-like comparison: "1 caught -> 3 caught" can mean
    # the plan got better, or that the same comment was split three ways and
    # all three were already covered. Scoring those together silently credits
    # a change for the judge changing its mind, so they are reported apart.
    resplit = [k for k in shared if pa[k][1] != pb[k][1]]
    comparable = [k for k in shared if pa[k][1] == pb[k][1]]

    better = [k for k in comparable if pb[k][0] > pa[k][0]]
    worse = [k for k in comparable if pb[k][0] < pa[k][0]]
    print(f"\n  Like-for-like ({len(comparable)} tickets, same problem count):")
    print(f"    improved: {len(better)}   regressed: {len(worse)}   "
          f"unchanged: {len(comparable) - len(better) - len(worse)}\n")
    for label, keys in (("IMPROVED", better), ("REGRESSED", worse)):
        if keys:
            print(f"    {label}:")
            for k in sorted(keys, key=lambda k: -abs(pb[k][0] - pa[k][0])):
                print(f"      {k}  {pa[k][0]} -> {pb[k][0]} of {pa[k][1]}")
            print()
    if resplit:
        print(f"    NOT COMPARABLE ({len(resplit)}) — the judge split the QA "
              f"comment differently:")
        for k in sorted(resplit):
            print(f"      {k}  {pa[k][0]}/{pa[k][1]}  ->  {pb[k][0]}/{pb[k][1]}")
        print()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("phase", choices=["replay", "grade", "score", "compare"])
    ap.add_argument("corpus", help="TSV of triage decisions")
    ap.add_argument("--limit", type=int, default=0,
                    help="only process this many (use a small number first)")
    ap.add_argument("--bounce-times", default=None,
                    help="mined corpus JSON, for exact bounce timestamps")
    ap.add_argument("--a", help="compare: baseline results dir")
    ap.add_argument("--b", help="compare: results dir to judge against it")
    args = ap.parse_args()

    rows = load_corpus(args.corpus, args.bounce_times)
    if args.phase == "replay":
        asyncio.run(phase_replay(rows, args.limit))
    elif args.phase == "grade":
        asyncio.run(phase_grade(rows, args.limit))
    elif args.phase == "compare":
        if not (args.a and args.b):
            sys.exit("compare needs --a and --b results directories")
        phase_compare(rows, args.a, args.b)
    else:
        phase_score(rows)


if __name__ == "__main__":
    main()
