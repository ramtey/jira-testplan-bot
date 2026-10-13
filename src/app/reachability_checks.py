"""Reachability rules L and M: a code branch is not user-reachable behaviour.

SK-2947's plan 627 had a happy-path case "CSS chunk failure reloads once". The
PR's detector matches ``/^Loading (CSS )?chunk .+ failed/``, and the generator
turned the ``(CSS )?`` alternative into a UI case. In that app (Next.js App
Router) route CSS loads through ``<link>`` tags. A 404 on one leaves the page
unstyled and throws nothing, and no CSS is lazily loaded, so nothing a tester
does produces that error. The case failed in UAT and the code was fine. The
same plan's integration case said "no CI configuration runs a test command"
while ``.github/workflows/ci.yml`` ran ``turbo run test`` for that package. That
claim came from a CI detector that did not know turbo, and the plan repeated it
without naming a file.

Rules L and M in ``SYSTEM_PROMPT`` tell the generator to name each UI case's
trigger and cite the file behind any claim about CI. This module checks both
after generation:

* **L, reachability critic:** every web_ui / mobile / manual_only case goes to
  a critic that can search and read the repo (the code explorer's tools). It
  answers, per case, whether a tester can produce the case's condition. A case
  it finds unreachable is rewritten where it stands. If a test the plan was
  given asserts the branch, the case becomes unit coverage
  (``covered_by_unit_test``), which the rule C/D checks then prove or revert.
  Otherwise it is marked and a follow-up asks for a test. "Unsure" changes
  nothing, so a case is never taken from a tester on a guess.
* **M, CI claims:** a case or regression line that says what CI does or does
  not run, without naming a CI file, is marked unverified. One that says a
  file is not run while the build facts show CI runs it is marked
  contradicted, with the file and command that run it.

Like scope_checks, nothing here moves a case between sections. A conversion
keeps its section and position, and a case QA already passed is only ever
marked, so the progress key keeps addressing the cases it was recorded on.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
from typing import Any, Iterable

from .code_exploration import Workspace, _new_result, run_tool_loop
from .diff_budget import allocate_patch_budget

logger = logging.getLogger(__name__)

CASE_SECTIONS = ("happy_path", "edge_cases", "integration_tests", "security_negative_tests")
UI_SURFACES = frozenset({"web_ui", "mobile", "manual_only"})

UNREACHABLE_MARKER = (
    "Not reachable in the running app — nothing a tester can do produces this "
    "condition, so this case cannot pass or fail on the change (rule L):"
)
CI_UNCITED_MARKER = "Unverified — this claim about CI names no CI file; check it before relying on it (rule M):"
CI_CONTRADICTED_MARKER = "Contradicted by the CI config — {why} (rule M):"

MAX_TURNS = 6
MAX_SEARCHES = 5
MAX_READS = 8
_DIFF_BUDGET = 24_000
_TEST_CHARS_PER_FILE = 6_000
_TEST_CHARS_TOTAL = 18_000


def _cases(test_plan: Any) -> Iterable[tuple[str, int, dict]]:
    for section in CASE_SECTIONS:
        for i, case in enumerate(getattr(test_plan, section, None) or []):
            if isinstance(case, dict):
                yield section, i, case


def _case_text(case: dict) -> str:
    parts = [case.get("title"), case.get("preconditions"), case.get("expected"),
             case.get("environment"), case.get("test_data"), case.get("unit_test_gap"),
             *(case.get("steps") or [])]
    return " ".join(p for p in parts if isinstance(p, str))


def _prefix(text: str, marker: str) -> str:
    return text if marker in text else f"{marker} {text}".rstrip()


# ---------------------------------------------------------------------------
# L. Reachability critic
# ---------------------------------------------------------------------------

REACHABILITY_SYSTEM_PROMPT = """You check one thing about manual QA test cases: can a tester actually produce the case's condition in the running app?

A plan is written from a diff. A branch in the diff (a regex alternative, a defensive guard, a fallback for an error, a handler for an input format) tells you what the code WOULD do. It does not mean anything in the app reaches it. A case that asks a tester for a condition the app never produces fails although the code is fine.

For each case:
1. Name the condition the case needs (the error, input or state) and the trigger it uses (its `trigger` field, preconditions and steps).
2. Check whether that trigger produces that condition in the build a tester runs. Use the framework's real behaviour as this repo uses it, and the repo itself: search for the producer (code that raises that error, sends that input, loads resources the way the branch assumes) with search_code, and read what you find with read_file. Do not take the branch's own comment or its unit test as evidence that the condition happens; a unit test builds the input by hand.
3. A tool that forces something (blocking a request in DevTools, editing storage, a proxy) counts only if it produces the SAME condition the code handles. A blocked request is a different failure from the one the code catches unless the framework turns one into the other.

Verdicts:
- "reachable": give the trigger and the path to the changed code, with file:line for hops you read.
- "unreachable": ONLY when the framework's behaviour or code you read shows nothing produces the condition. Say what you searched and read and what it showed. If one of the TEST FILES below asserts that branch, also give `unit_test_ref` as "<file> › <exact test name as written>", `coverage_evidence` as the assertion line(s) copied verbatim from that test's body, and a unit-level rewrite of the case: `unit_title` and `unit_expected` state only what that test proves (no UI outcome), `unit_steps` say how to run that test file.
- "unsure": anything you cannot settle. This is the default. Never answer "unreachable" on a guess.

Judge reachability only, never whether the code is correct. Call report_reachability exactly once, with one verdict per case id."""

REPORT_REACHABILITY_TOOL = {
    "name": "report_reachability",
    "description": "Report, for every case id you were given, whether a tester can produce its condition.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["reachable", "unreachable", "unsure"]},
                        "trigger": {"type": "string", "description": "For reachable: the user action or setup step and the path to the changed code."},
                        "reason": {"type": "string", "description": "What you searched or read and what it showed."},
                        "unit_test_ref": {"type": "string"},
                        "coverage_evidence": {"type": "string"},
                        "unit_title": {"type": "string"},
                        "unit_expected": {"type": "string"},
                        "unit_steps": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "verdict", "reason"],
                },
            }
        },
        "required": ["verdicts"],
    },
}


def collect_reachability_items(test_plan: Any) -> list[dict]:
    """The cases a tester performs in the app: UI surfaces, not unit-covered."""
    items = []
    for section, i, case in _cases(test_plan):
        if case.get("surface") not in UI_SURFACES or case.get("covered_by_unit_test"):
            continue
        items.append({"id": f"{section}:{i}", "_case": case})
    return items


def _test_sources(repo_evidence: list[dict] | None) -> list[tuple[str, str, str]]:
    """(path, CI status line, text) for each test file the plan was given."""
    out, budget = [], _TEST_CHARS_TOTAL
    for e in repo_evidence or []:
        suites = ((e.get("build") or {}).get("suites")) or {}
        for path, text in (e.get("test_sources") or {}).items():
            if budget <= 0:
                return out
            st = suites.get(path) or {"status": "unknown", "why": "not checked"}
            body = text[:min(_TEST_CHARS_PER_FILE, budget)]
            budget -= len(body)
            out.append((path, f"{st['status'].upper()} ({st['why']})", body))
    return out


def build_reachability_message(items: list[dict], pull_requests: list[dict],
                               repo_evidence: list[dict] | None,
                               exploration: dict | None) -> str:
    parts = ["## CASES"]
    for item in items:
        c = item["_case"]
        steps = "\n".join(f"  {n}. {s}" for n, s in enumerate(c.get("steps") or [], 1))
        parts.append(
            f"### {item['id']} ({c.get('surface')})\nTitle: {c.get('title')}\n"
            f"Preconditions: {c.get('preconditions') or '-'}\nTrigger (as the plan states it): "
            f"{c.get('trigger') or '(none given)'}\nSteps:\n{steps}\nExpected: {c.get('expected')}"
        )
    parts.append("## PULL REQUESTS AND DIFFS (may be cut to fit)")
    per_pr = _DIFF_BUDGET // max(1, len(pull_requests))
    for pr in pull_requests:
        parts.append(f"- {pr.get('repository')} #{pr.get('number')}: {pr.get('title') or ''}")
        for name, patch, cut in allocate_patch_budget(pr.get("files_changed") or [],
                                                      total_budget=per_pr, per_file_cap=6000):
            parts.append(f"--- {pr.get('repository')}:{name}{' (cut)' if cut else ''}\n{patch}")
    tests = _test_sources(repo_evidence)
    if tests:
        parts.append("## TEST FILES (at the merge commit) — the only tests you may cite")
        for path, ci, body in tests:
            parts.append(f"--- TEST FILE {path} — CI status: {ci} ---\n{body}")
    findings = (exploration or {}).get("findings") or []
    if findings:
        parts.append("## WHAT THE CODE EXPLORER FOUND OUTSIDE THE DIFFS")
        for f in findings:
            parts.append(f"- [{f.get('kind')}] {f.get('repo')}:{f.get('path')}:{f.get('lines')} — "
                         f"{f.get('what_it_does')}")
    return "\n\n".join(parts)


def parse_reachability_verdicts(raw: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for v in (raw or {}).get("verdicts") or []:
        if isinstance(v, dict) and v.get("verdict") in ("reachable", "unreachable", "unsure"):
            out[str(v.get("id"))] = v
    return out


def _owner(pull_requests: list[dict]) -> str:
    repos = sorted({pr.get("repository") for pr in pull_requests if pr.get("repository")})
    return f"Owners of {', '.join(repos)}" if repos else "The team that owns the changed code"


# Fields that describe the UI run a converted case no longer has.
_UI_ONLY_FIELDS = ("trigger", "environment", "data_shape", "step_grounding", "unverified_steps",
                   "needs_manual_verification")


def apply_reachability_verdicts(test_plan: Any, items: list[dict], verdicts: dict[str, dict],
                                *, protected_titles: frozenset[str] = frozenset(),
                                pull_requests: list[dict] | None = None) -> dict:
    """Apply each verdict at the case's own id. Returns what changed.

    ``protected_titles`` are cases QA already passed: those are marked, never
    rewritten, so a recorded result keeps describing the case it was recorded on.
    """
    converted, marked, confirmed = [], [], []
    gaps = getattr(test_plan, "risks_and_gaps", None)
    if gaps is None:
        gaps = []
        test_plan.risks_and_gaps = gaps
    for item in items:
        v = verdicts.get(item["id"])
        case = item["_case"]
        if not v or v["verdict"] == "unsure":
            continue
        if v["verdict"] == "reachable":
            if not (case.get("trigger") or "").strip() and (v.get("trigger") or "").strip():
                case["trigger"] = v["trigger"].strip()
            confirmed.append(item["id"])
            continue
        reason = (v.get("reason") or "").strip() or "the critic found nothing that produces this condition"
        record = {"reachable": False, "reason": reason,
                  "original_title": case.get("title"), "original_expected": case.get("expected")}
        has_test = all((v.get(k) or "").strip() for k in ("unit_test_ref", "coverage_evidence",
                                                           "unit_title", "unit_expected"))
        if has_test and (case.get("title") or "") not in protected_titles:
            for k in _UI_ONLY_FIELDS:
                case.pop(k, None)
            steps = [s for s in (v.get("unit_steps") or []) if isinstance(s, str) and s.strip()]
            case.update(
                title=v["unit_title"].strip(),
                expected=v["unit_expected"].strip(),
                steps=steps or [f"Run the test {v['unit_test_ref'].strip()}."],
                test_data=f"Not reachable from the UI: {reason}",
                surface="cli",
                covered_by_unit_test=True,
                unit_test_ref=v["unit_test_ref"].strip(),
                coverage_evidence=v["coverage_evidence"].strip(),
                reachability={**record, "converted_to_unit_coverage": True},
            )
            converted.append(item["id"])
            continue
        case["expected"] = _prefix(case.get("expected") or "", f"{UNREACHABLE_MARKER} {reason} —")
        case["reachability"] = record
        if has_test:
            case["reachability"]["unit_test_ref"] = v["unit_test_ref"].strip()
        elif isinstance(gaps, list):
            gaps.append({
                "gap": f"No test covers a branch nothing in the app reaches: {record['original_title']}",
                "impact": "The branch is untested, and no tester can exercise it, so a regression in it would ship unseen.",
                "evidence": reason,
                "follow_up": True,
                "owner": _owner(pull_requests or []),
                "suggested_ticket": f"Add a unit test for: {record['original_title']}",
            })
        marked.append(item["id"])
    return {"converted": converted, "marked": marked, "confirmed": confirmed}


async def run_reachability_critic(llm: Any, github: Any, test_plan: Any,
                                  dev_infos: Iterable[dict | None], *,
                                  repo_evidence: list[dict] | None = None,
                                  exploration: dict | None = None,
                                  protected_titles: frozenset[str] = frozenset()) -> tuple[dict, str | None]:
    """Rule L. Returns ``(changes, gap)``; ``gap`` is set when the critic could
    not run, so the plan says no reachability check happened."""
    changes = {"converted": [], "marked": [], "confirmed": []}
    items = collect_reachability_items(test_plan)
    if not items:
        return changes, None
    prs = [pr for dev in dev_infos for pr in ((dev or {}).get("pull_requests") or [])
           if isinstance(pr, dict) and pr.get("repository")]
    if not prs or not hasattr(llm, "_post_messages"):
        return changes, None
    result = _new_result()
    ws = Workspace(github, prs, max_searches=MAX_SEARCHES, max_reads=MAX_READS)
    try:
        raw = await run_tool_loop(
            llm, ws, system=REACHABILITY_SYSTEM_PROMPT,
            first_message=build_reachability_message(items, prs, repo_evidence, exploration),
            report_tool=REPORT_REACHABILITY_TOOL, max_turns=MAX_TURNS, result=result)
    except Exception as e:  # never fails the plan; the gap says what was skipped
        logger.warning("reachability critic failed: %s", e, exc_info=True)
        return changes, f"the reachability critic could not run ({type(e).__name__}), so {len(items)} UI case(s) were not checked for a reachable trigger"
    if raw is None:
        return changes, f"the reachability critic {result['error']}, so {len(items)} UI case(s) were not checked for a reachable trigger"
    changes = apply_reachability_verdicts(test_plan, items, parse_reachability_verdicts(raw),
                                          protected_titles=protected_titles, pull_requests=prs)
    changes["usage"] = result["usage"]
    changes["searches"], changes["reads"] = ws.searches, ws.reads
    return changes, None


# ---------------------------------------------------------------------------
# M. Claims about CI cite a CI file
# ---------------------------------------------------------------------------

_CI_WORD = r"(?:CI|continuous integration|GitHub Actions|workflows?|pipelines?|CI jobs?)"
_CI_CLAIM = re.compile(
    rf"\b(?:no|not|never|nothing in)\b[^.;]{{0,40}}\b{_CI_WORD}\b"
    rf"|\b{_CI_WORD}\b[^.;]{{0,40}}\b(?:does not|doesn't|do not|don't|never|not)\b[^.;]{{0,25}}\b(?:run|execute|cover)"
    rf"|\b{_CI_WORD}\b[^.;]{{0,30}}\b(?:runs|executes|covers|enforces)\b"
    rf"|\b(?:run|executed|enforced|covered)\s+(?:by|in)\s+(?:the\s+)?{_CI_WORD}\b",
    re.I,
)
_CI_NEGATIVE = re.compile(
    rf"\b(?:no|not|never|nothing in)\b[^.;]{{0,40}}\b{_CI_WORD}\b"
    rf"|\b{_CI_WORD}\b[^.;]{{0,40}}\b(?:does not|doesn't|do not|don't|never|not)\b",
    re.I,
)
_CI_FILE_CITED = re.compile(
    r"\.github/workflows/|\bJenkinsfile\b|\.gitlab-ci\.yml|\b[\w.-]*ci[\w.-]*\.ya?ml\b|buildspec"
    r"|azure-pipelines|bitbucket-pipelines|\.circleci/|\.buildkite/|\bDockerfile\b",
    re.I,
)
# "on Linux CI" is rule G naming a platform, not a claim about what CI runs.
_PLATFORM_CI = re.compile(r"\b(?:on|in)\s+(?:the\s+)?(?:Linux\s+)?CI\b(?!\s+(?:runs|executes|covers))", re.I)


def _ci_claim(text: str) -> bool:
    return bool(_CI_CLAIM.search(_PLATFORM_CI.sub(" ", text)))


def _contradiction(text: str, suites: dict[str, dict]) -> str | None:
    """The CI-run evidence for a test file the text says CI does not run."""
    if not _CI_NEGATIVE.search(_PLATFORM_CI.sub(" ", text)):
        return None
    for path, st in suites.items():
        if st.get("status") == "runs" and (path in text or posixpath.basename(path) in text):
            return st.get("why") or f"CI runs {path}"
    return None


def _suites(repo_evidence: list[dict] | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in repo_evidence or []:
        out.update(((e.get("build") or {}).get("suites")) or {})
    return out


def _insert_after_emoji(line: str, marker: str) -> str:
    m = re.match(r"^(\s*(?:🔴|🟡|🟢)\s*)", line)
    head = m.group(1) if m else ""
    return f"{head}{marker} {line[len(head):]}"


def flag_ci_claims(test_plan: Any, repo_evidence: list[dict] | None) -> list[dict]:
    """Rule M, for CI. Marks the expected result (or the regression line) of
    every claim about what CI runs that cites no CI file, and of every claim
    that a test file is not run when the build facts show CI runs it."""
    suites = _suites(repo_evidence)
    flagged: list[dict] = []

    def verdict(text: str) -> tuple[str, str] | None:
        why = _contradiction(text, suites)
        if why:
            return "contradicted", CI_CONTRADICTED_MARKER.format(why=why)
        if _ci_claim(text) and not _CI_FILE_CITED.search(text):
            return "uncited", CI_UNCITED_MARKER
        return None

    for section, i, case in _cases(test_plan):
        found = verdict(_case_text(case))
        if found and found[1] not in (case.get("expected") or ""):
            case["expected"] = _prefix(case.get("expected") or "", found[1])
            flagged.append({"location": f"{section}:{i}", "kind": found[0]})
    lines = getattr(test_plan, "regression_checklist", None) or []
    for i, line in enumerate(lines):
        if isinstance(line, str):
            found = verdict(line)
            if found and found[1] not in line:
                lines[i] = _insert_after_emoji(line, found[1])
                flagged.append({"location": f"regression_checklist:{i}", "kind": found[0]})
    for gap in getattr(test_plan, "risks_and_gaps", None) or []:
        if isinstance(gap, dict):
            text = " ".join(str(gap.get(k) or "") for k in ("gap", "impact", "evidence"))
            found = verdict(text)
            if found and found[0] == "contradicted" and found[1] not in str(gap.get("gap")):
                gap["gap"] = f"{found[1]} {gap.get('gap') or ''}".strip()
                flagged.append({"location": "risks_and_gaps", "kind": found[0]})
    return flagged


def summarize(changes: dict) -> str:
    return json.dumps({k: changes.get(k) for k in ("converted", "marked", "confirmed", "searches", "reads")})
