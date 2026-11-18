"""Re-check the plan against rules Q–Z before it is published.

A UAT run on an Android ticket found a plan that predicted the wrong results,
missed a real defect and graded cases on code the change never touched:

* an expected "blank screen" for a failure thrown by a call that sat outside
  the ``runCatching`` meant to protect it (the app showed an error alert);
* "the failure is reported" where the function now returned ``false`` and no
  caller read it;
* "the tests confirm the order" where no test asserted the order;
* "previously, files were named …" read from an intermediate commit of the
  ticket's own PRs instead of the code before the first PR;
* graded cases on behaviour in files the diff did not touch, one of them
  labelled by the plan itself as "unverified against shipped code";
* a case needing two documents with the same file name, which production's
  GUID-suffixed URLs cannot produce;
* regression lines with no steps, and toast results with no hint to record;
* ``gradle test`` and a bare ``applicationId`` where the repo has a wrapper
  and a debug ``applicationIdSuffix``;
* the build check pasted into every case.

The prompt now has rules Q–Z for these. This module is the other half: it does
not trust that the generator followed them. Two passes:

``recheck_before_routing`` (async; before ``route_out_of_uat``)
    **U** — a graded case whose every code citation is outside the diff and
    does not call changed code is made a pre-existing note, and one the plan
    itself calls "unverified against shipped code" leaves for the code-review
    list. **Q/T** — an agentic critic re-traces each failure-path case and each
    "before this change" claim (at the ticket base). A contradicted Expected is
    REPLACED by the traced outcome, never kept beside it (SK-3060), and when the
    steps cannot reach the condition the title names, the case is refit to a
    traced trigger, or split with the intent listed for code review.

``recheck_before_save`` (sync; after the scope checks, before the grounding
re-read and quarantine)
    **Q** an Expected that keeps a contradicted claim beside its correction,
    and a file or step name cut short of the code's name; an exact error
    message, status or error class with no traced path to
    the first check that rejects that input — per table row
    (``first_rejection.check_pinned_messages``), **S** "tests confirm" without an assertion, **R** "reported / logged /
    surfaced" without the reporting line, **X** a case or regression line a
    tester cannot run or check (and brief UI with no "record the screen"),
    **Y** commands and identifiers against the build config
    (``build_commands.check_commands``), **Z** setup repeated across cases is
    written once in ``shared_preconditions``.

Like every pass since the progress key, these **mark in place** and never
reorder. The two moves (rule U) happen before ``route_out_of_uat``, the same
place rules H and L move cases, and spare cases QA already passed. Every
downgrade is recorded on the case (``grounding_downgrades``, which the UI
shows) and in ``source_provenance["plan_recheck"]``, which the plan's
"Grounding re-read" line lists one by one. A check that could not run says so
there; "could not check" is never "passed".
"""

from __future__ import annotations

import logging
import posixpath
import re
from typing import Any, Iterable

logger = logging.getLogger(__name__)

GRADED_SECTIONS = ("happy_path", "edge_cases", "integration_tests")

TEST_CLAIM_MARKER = "No test asserts this (rule S) — from reading the code{src}:"
SIGNAL_MARKER = "Unverified — no line shown that reports this (rule R):"
RUNNABLE_MARKER = "Not runnable as written (rule X): {what} —"
CONTRADICTED_MARKER = "Contradicted by the code (rule Q): {why} —"
BEFORE_MARKER = "Unverified before-behaviour (rule T): {why} —"
OUT_OF_DIFF_LABEL = "Pre-existing behaviour, not changed by this ticket:"
BRIEF_UI_NOTE = ("This result is on screen only briefly (a toast, snackbar or alert that disappears after a "
                 "few seconds): record the screen while running the steps.")


def _cases(test_plan: Any, sections: Iterable[str] = GRADED_SECTIONS):
    for section in sections:
        for i, case in enumerate(getattr(test_plan, section, None) or []):
            if isinstance(case, dict):
                yield section, i, case


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?;])\s+(?=[A-Z0-9\"'`(])", text or "") if s.strip()]


def _record(report: dict, case: dict | None, section: str, rule: str, action: str, reason: str,
            claim: str = "") -> None:
    title = (case or {}).get("title") if isinstance(case, dict) else None
    entry = {"section": section, "title": (title or claim or "")[:140], "rule": rule,
             "action": action, "reason": reason}
    report.setdefault("findings", []).append(entry)
    if isinstance(case, dict) and action in ("downgraded", "marked", "moved"):
        case.setdefault("grounding_downgrades", []).append(
            {"claim": f"rule {rule}: {claim or action}"[:200], "reason": reason})


def _protected(case: dict, protected_titles: frozenset[str]) -> bool:
    if not protected_titles:
        return False
    from .case_disposition import _protected as protected

    return protected(case, protected_titles)


_FILE_LINE_RE = re.compile(r"[\w./-]+\.[A-Za-z]{1,6}:\d+")


# ---------------------------------------------------------------------------
# S. "Tests confirm" needs an assertion
# ---------------------------------------------------------------------------

_TEST_CLAIM_RE = re.compile(
    r"\b(?:unit\s+|integration\s+|existing\s+|the\s+)?tests?\b[^.;]{0,40}?\b(?:confirm|confirms|prove|proves|"
    r"verif(?:y|ies)|assert|asserts|ensure|ensures|show|shows|guarantee|guarantees|cover|covers|check|checks)\b"
    r"|\b(?:covered|confirmed|proven|verified|asserted)\s+by\s+(?:the\s+|a\s+|existing\s+)?(?:unit\s+)?tests?\b",
    re.I,
)
_ASSERTION_CITED_RE = re.compile(r"›|\bassert\w*\s*\(|\bexpect\s*\(|\bverify\s*\(|\bXCTAssert|\bassertThat\b")


def _proven(case: dict) -> bool:
    """The case carries an assertion the evidence checks kept (rules C/D)."""
    return bool(case.get("covered_by_unit_test") and (case.get("coverage_evidence") or "").strip()
                and (case.get("unit_test_ref") or "").strip())


def check_test_claims(test_plan: Any, report: dict) -> int:
    n = 0
    for section, _, case in _cases(test_plan):
        if _proven(case):
            continue
        src = case.get("expected_source") or ""
        marker = TEST_CLAIM_MARKER.format(src=f" ({src})" if src else "")
        for key in ("expected", "how_to_verify"):
            text = case.get(key)
            if not isinstance(text, str) or "(rule S)" in text:
                continue
            out, hit = [], None
            for s in _sentences(text):
                if _TEST_CLAIM_RE.search(s) and not _ASSERTION_CITED_RE.search(s) and not re.search(
                        r"\bno test\b|\bnot (?:asserted|covered)\b|\bwithout a test\b", s, re.I):
                    hit = hit or s
                    out.append(f"{marker} {s}")
                else:
                    out.append(s)
            if hit:
                case[key] = " ".join(out)
                n += 1
                _record(report, case, section, "S", "marked",
                        "claims a test confirms it, but no assertion is cited for it", hit[:160])
    return n


# ---------------------------------------------------------------------------
# R. "Reported / logged / surfaced" needs the reporting line
# ---------------------------------------------------------------------------

_SIGNAL_RE = re.compile(
    r"\b(?:is|are|gets?|got|was|were|be|been|being)\s+(?:also\s+)?(?:reported|surfaced|logged|propagated|"
    r"captured|tracked|recorded\s+(?:in|to)|sent\s+to|shown\s+to\s+the\s+user|forwarded\s+to)\b"
    r"|\b(?:reports?|logs?|surfaces?|propagates?)\s+(?:the|an?|this|that)\s+(?:error|failure|exception|"
    r"problem|result|status)\b"
    r"|\b(?:to|in)\s+(?:Sentry|Crashlytics|Datadog|Bugsnag|Amplitude|Mixpanel|Segment|New Relic|Firebase)\b",
    re.I,
)
_REPORTING_CALL_RE = re.compile(
    r"\b(?:log|logger|Log\.[a-z]|Timber|console\.|print|NSLog|os_log|Sentry|captureException|captureMessage|"
    r"Crashlytics|recordException|report|track|analytics|emit|send|post|publish|dispatch|notify|toast|"
    r"Toast|Snackbar|alert|Alert|showError|setError|error|throw|raise|onError|callback|Result\.failure|"
    r"_?state\.|\.value\s*=|emit\(|setState|return)\b",
    re.I,
)


def _line_text(index, locator: str) -> str | None:
    """The text at ``<file>:<line>`` from the source index; None when unread."""
    if index is None or isinstance(index, str):
        return None
    m = re.match(r"(.+?):(\d+)(?:-(\d+))?$", locator.strip().strip("`"))
    if not m:
        return None
    try:
        got = index.resolve(m.group(1))
    except Exception:
        return None
    text = getattr(got, "text", None)
    if not isinstance(text, str):
        return None
    lines = text.splitlines()
    a = int(m.group(2))
    b = int(m.group(3) or a)
    if a < 1 or a > len(lines):
        return ""
    return "\n".join(lines[a - 1:min(b, len(lines))])


def check_signal_claims(test_plan: Any, report: dict, index=None) -> int:
    n = 0
    for section, _, case in _cases(test_plan):
        sources = [s for s in case.get("signal_sources") or [] if isinstance(s, dict)]
        # A cited reporting line must exist and look like it reports something.
        good_sources = []
        for s in sources:
            line = (s.get("line") or "").strip()
            text = _line_text(index, line) if line else None
            if not line or not _FILE_LINE_RE.search(line):
                _record(report, case, section, "R", "downgraded",
                        "signal_sources entry names no <file>:<line>", s.get("claim") or "")
                continue
            if text == "" or (text is not None and not _REPORTING_CALL_RE.search(text)):
                _record(report, case, section, "R", "downgraded",
                        f"{line} does not report anything" + (f" (it reads: {text.strip()[:80]})" if text else
                                                              " (no such line)"), s.get("claim") or "")
                continue
            good_sources.append(s)
        text = case.get("expected")
        if not isinstance(text, str) or "(rule R)" in text:
            continue
        out, hit = [], None
        for s in _sentences(text):
            if _SIGNAL_RE.search(s) and not _FILE_LINE_RE.search(s) and not good_sources and not re.search(
                    r"\bnot\s+(?:reported|logged|surfaced)\b|\bnever\b|\bno\s+(?:log|report)", s, re.I):
                hit = hit or s
                out.append(f"{SIGNAL_MARKER} {s}")
            else:
                out.append(s)
        if hit:
            case["expected"] = " ".join(out)
            case["expected_verified"] = False
            n += 1
            _record(report, case, section, "R", "downgraded",
                    "says it is reported/logged/surfaced but names no line that does it", hit[:160])
    return n


# ---------------------------------------------------------------------------
# X. Runnable and checkable
# ---------------------------------------------------------------------------

_BRIEF_UI_RE = re.compile(r"\b(?:toast|snack\s?bar|transient|briefly|flash(?:es)?\s+(?:a|the)|"
                          r"disappears?\s+after|auto[- ]?dismiss)", re.I)
_RECORD_RE = re.compile(r"\brecord(?:ing)?\s+(?:the|a|your)?\s*screen|\bscreen\s*record", re.I)
_REG_STEPS_RE = re.compile(r"\bSteps?:\s*1[.)]|\b1[.)]\s+\S", re.I)
_REG_EXPECTED_RE = re.compile(r"\bExpected(?:\s+result)?:", re.I)
_REG_CHECK_RE = re.compile(r"\b(?:Check|Verify(?:\s+by|\s+in|\s+at)?|Confirm\s+in|Look\s+at|Where\s+to\s+look):", re.I)


def check_runnable(test_plan: Any, report: dict) -> int:
    n = 0
    for section, _, case in _cases(test_plan, GRADED_SECTIONS + ("security_negative_tests",)):
        steps = [s for s in case.get("steps") or [] if isinstance(s, str) and s.strip()]
        expected = (case.get("expected") or "").strip()
        how = (case.get("how_to_verify") or "").strip()
        missing = [w for w, bad in (("no steps", not steps), ("no expected result", not expected),
                                    ("no place to check the result (how_to_verify)",
                                     not how and not case.get("covered_by_unit_test"))) if bad]
        if missing and "(rule X)" not in expected:
            case["expected"] = f"{RUNNABLE_MARKER.format(what=', '.join(missing))} {expected}".strip()
            n += 1
            _record(report, case, section, "X", "marked", ", ".join(missing))
        text = " ".join([expected, how] + steps)
        if _BRIEF_UI_RE.search(text) and not _RECORD_RE.search(text):
            case["how_to_verify"] = f"{how} {BRIEF_UI_NOTE}".strip()
            _record(report, case, section, "X", "fixed", "brief UI: added 'record the screen'")
    lines = getattr(test_plan, "regression_checklist", None) or []
    for i, line in enumerate(lines):
        if not isinstance(line, str) or "(rule X)" in line:
            continue
        missing = [w for w, rx in (("numbered steps", _REG_STEPS_RE), ("an expected result", _REG_EXPECTED_RE),
                                   ("where to check", _REG_CHECK_RE)) if not rx.search(line)]
        if missing:
            m = re.match(r"^(\s*(?:🔴|🟡|🟢)\s*)", line)
            head = m.group(1) if m else ""
            lines[i] = f"{head}{RUNNABLE_MARKER.format(what='no ' + ', no '.join(missing))} {line[len(head):]}"
            n += 1
            _record(report, None, "regression_checklist", "X", "marked", "no " + ", no ".join(missing),
                    line[len(head):][:140])
    return n


# ---------------------------------------------------------------------------
# Z. Shared setup written once
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def hoist_shared_preconditions(test_plan: Any, report: dict, min_cases: int = 2) -> int:
    """Move a precondition sentence repeated in ``min_cases``+ cases to
    ``shared_preconditions``, and refer to it from each case."""
    from .evidence_checks import BUILD_CHECK_PRECONDITION

    shared = [s for s in (getattr(test_plan, "shared_preconditions", None) or []) if isinstance(s, dict)]
    seen: dict[str, list[tuple[dict, str]]] = {}
    for _, _, case in _cases(test_plan):
        pre = case.get("preconditions")
        if not isinstance(pre, str):
            continue
        for s in _sentences(pre):
            if (s.startswith("Precondition:") or BUILD_CHECK_PRECONDITION in s or len(s) < 25
                    or "(rule AF)" in s):
                continue
            seen.setdefault(_norm(s), []).append((case, s))
    hoisted = 0
    taken = {str(s.get("id")) for s in shared}
    for key, hits in seen.items():
        cases = {id(c) for c, _ in hits}
        if len(cases) < min_cases:
            continue
        existing = next((s for s in shared if _norm(s.get("text") or "") == key), None)
        if existing is None:
            n = len(shared) + 1
            while f"S{n}" in taken:
                n += 1
            existing = {"id": f"S{n}", "text": hits[0][1].strip()}
            taken.add(existing["id"])
            shared.append(existing)
        ref = f"Precondition: {existing['id']} passed (top of plan)."
        for case, s in hits:
            pre = case["preconditions"]
            if ref not in pre:
                case["preconditions"] = pre.replace(s, ref, 1)
            else:
                case["preconditions"] = re.sub(r"\s{2,}", " ", pre.replace(s, "", 1)).strip()
        hoisted += 1
        _record(report, None, "shared_preconditions", "Z", "fixed",
                f"setup repeated in {len(cases)} cases written once as {existing['id']}", existing["text"])
    if shared:
        test_plan.shared_preconditions = shared
    return hoisted


# ---------------------------------------------------------------------------
# U. Graded cases test code the change touched
# ---------------------------------------------------------------------------

_UNVERIFIED_SHIPPED_RE = re.compile(
    r"\b(?:unverified|not\s+verified|cannot\s+be\s+verified|could\s+not\s+(?:be\s+)?verif(?:y|ied))\s+"
    r"(?:against|in|on)\s+(?:the\s+)?(?:shipped|released|production|merged)\s+(?:code|build|app)", re.I)
_PATH_RE = re.compile(r"(?<![\w/])((?:[\w.-]+/)*[\w.-]+\.(?:kt|kts|java|swift|m|mm|ts|tsx|js|jsx|py|rb|go|cs|"
                      r"php|dart|scala|rs|vue|svelte|xml|gradle))(?::\d+(?:-\d+)?)?")


def _cited_code_paths(case: dict) -> list[str]:
    texts = [case.get("expected_source") or "", case.get("traced_path") or ""]
    texts += [g[3:] if isinstance(g, str) and g.startswith("pr:") else "" for g in case.get("grounded_in") or []]
    return list(dict.fromkeys(m.group(1) for t in texts for m in _PATH_RE.finditer(t)))


def _changed_symbols(changed: list[str]) -> set[str]:
    """Names a direct caller of changed code would mention: changed files' stems."""
    out = set()
    for c in changed:
        stem = posixpath.splitext(posixpath.basename(c))[0]
        if len(stem) >= 5 and stem.lower() not in ("index", "utils", "types", "constants", "styles"):
            out.add(stem)
    return out


async def check_graded_scope(test_plan: Any, dev_infos: list, report: dict, *, index=None, github=None,
                             exploration: dict | None = None,
                             protected_titles: frozenset[str] = frozenset()) -> dict:
    """Rule U. Returns ``{"moved_pre_existing", "moved_code_review", "unchecked"}`` (titles)."""
    from .build_targets import path_matches
    from .case_disposition import _call_sites, changed_paths, regression_anchor

    out = {"moved_pre_existing": [], "moved_code_review": [], "unchecked": []}
    changed = changed_paths(dev_infos)
    sites = _call_sites(exploration)
    patches = "\n".join(f.get("patch") or "" for dev in dev_infos
                        for pr in ((dev or {}).get("pull_requests") or []) if isinstance(pr, dict)
                        for f in (pr.get("files_changed") or []) if isinstance(f, dict))
    symbols = _changed_symbols(changed)
    pending: list[tuple[str, dict, list[str]]] = []
    for section, _, case in _cases(test_plan):
        if _protected(case, protected_titles) or case.get("pre_existing") or case.get("covered_by_unit_test"):
            continue
        text = " ".join([case.get("expected") or "", case.get("title") or "", case.get("test_data") or ""])
        if _UNVERIFIED_SHIPPED_RE.search(text):
            case["not_gradeable"] = {
                "reason": "the plan itself marks this unverified against the shipped code, so a tester "
                          "cannot grade it (rule U)"}
            out["moved_code_review"].append(case.get("title") or "")
            _record(report, case, section, "U", "moved", "marked unverified against shipped code; moved to "
                    "verify by code review")
            continue
        if not changed:
            continue
        paths = _cited_code_paths(case)
        if not paths:
            continue
        outside = [p for p in paths if not regression_anchor(p, changed, sites, patches)
                   and not any(path_matches(p, c) for c in changed)]
        if len(outside) == len(paths):
            pending.append((section, case, outside))
    if not pending:
        return out
    # A cited file that mentions a changed file's class or module calls it
    # directly; read each once (at the PR head when it is in a PR repo).
    texts: dict[str, str | None] = {}
    if index is not None and not isinstance(index, str) and github is not None:
        from .source_facts import read_cited

        try:
            await read_cited(index, github, sorted({p for _, _, ps in pending for p in ps}))
        except Exception as e:  # unread is "unchecked", never "outside the diff"
            logger.warning("rule U: cited files unreadable: %s", e)
        for _, _, ps in pending:
            for p in ps:
                try:
                    got = index.resolve(p)
                except Exception:
                    got = None
                texts[p] = getattr(got, "text", None) if got is not None and not isinstance(got, list) else None
    for section, case, ps in pending:
        unread = [p for p in ps if texts.get(p) is None]
        if unread:
            out["unchecked"].append(case.get("title") or "")
            report.setdefault("unchecked", []).append(
                f"rule U: {case.get('title') or section} cites {', '.join(unread[:2])}, which could not be read")
            continue
        if any(re.search(rf"\b{re.escape(sym)}\b", texts[p] or "") for p in ps for sym in symbols):
            continue  # a direct caller of changed code
        reason = (f"it cites only {', '.join(ps[:3])}, which the change did not touch and which do not call "
                  f"changed code (rule U)")
        case["pre_existing"] = True
        exp = case.get("expected") or ""
        if not exp.startswith(OUT_OF_DIFF_LABEL):
            case["expected"] = f"{OUT_OF_DIFF_LABEL} {exp}".strip()
        case["out_of_diff"] = {"cited": ps, "reason": reason}
        out["moved_pre_existing"].append(case.get("title") or "")
        _record(report, case, section, "U", "moved", f"graded case on untouched code: {reason}")
    return out


# ---------------------------------------------------------------------------
# Q/T. The traced path and the old code — an agentic critic
# ---------------------------------------------------------------------------

_FAILURE_RE = re.compile(r"\b(?:error|errors|fail(?:s|ed|ure|ing)?|exception|throws?|crash(?:es)?|null|nil|"
                         r"missing|not\s+found|offline|time[sd]?\s?out|invalid|denied|empty|blank|alert|"
                         r"retry|corrupt|unavailable|rejected?)\b", re.I)
_BEFORE_RE = re.compile(r"\b(?:previously|before\s+(?:this|the)\s+(?:change|fix|ticket|PR)|used\s+to|"
                        r"old\s+behaviou?r|prior\s+to\s+(?:this|the)\s+(?:change|fix)|the\s+old\s+code|"
                        r"formerly|no\s+longer)\b", re.I)
MAX_ITEMS = 10
MAX_TURNS = 6
MAX_SEARCHES = 6
MAX_READS = 14

TRACE_SYSTEM_PROMPT = """You re-check manual QA test cases against the code before the plan is published. You check two things and nothing else.

1. ERROR PATH (kind "error_path"). For a case about a failure (an error, a missing record, null, a crash, offline, a timeout), trace what the code really does, hop by hop, from the trigger to what the user sees. Read the files; do not infer.
   - Find the call that fails and how it fails (throws, returns null, returns false, a Result.failure, an error code).
   - Check whether that call is INSIDE the try/catch, runCatching, .catch, rescue or error boundary that seems to protect it. A call placed before or outside it throws past it and takes a different route: an error alert, a crash, a generic error screen.
   - A nullable return type does not mean "returns null on failure". Find the line that turns each failure into null, or note that nothing does.
   - When the code catches a failure on purpose and turns it into a value (a missing row caught and returned as null), the user does not see a crash.
   Verdicts: "consistent" when the case's expected result matches what the traced path shows; "contradicted" when the code shows a different visible result (say which, with file:line for each hop you read, including the handler that produces it); "unverifiable" when you could not read what you needed.
   For every "contradicted" item, also:
   - `corrected_expected`: the whole Expected a tester grades for THESE steps, rewritten from your trace. It replaces the case's Expected, so it states only what the steps really produce and never repeats the contradicted claim. Quote every file, step, field and function name exactly as the code spells it, whole, never shortened.
   - `steps_reach_intent`: does the trace reach the condition the case's TITLE is about? false when the failure happens before the component under test runs (an early validation or lookup fails, so the step the title is about is never sent or never runs).
   - When `steps_reach_intent` is false: `actual_title` (a title for what these steps really test), and either `reachable_trigger` (steps and test data a tester can really drive that DO reach the title's condition, traced in the code you read, with its own expected result) or `unreachable_reason` (why nothing a tester can drive reaches it, and the unit test or code review that would check it). Never invent a trigger you did not trace.

2. BEFORE-BEHAVIOUR (kind "before"). For a claim about how the code behaved before this change, read the file at the ticket base (read_file with at="base") or the '-' lines of the first PR's diff, and compare. Verdicts: "consistent" (quote the old line), "contradicted" (quote the old line that says otherwise), "unverifiable".

3. FIRST REJECTING CHECK (items that list "Pinned error text"). The case quotes an exact error message, HTTP status or error class. Do not check it against the one function the case cites. Start at the real entry point a tester's input reaches (the handler, the exported function the harness or route calls) and walk it IN EXECUTION ORDER: input validation, parsing and encoding (URL parsing, JSON decode), auth, allow-lists and policy checks, decryption or comparison, then the target function. The first check that rejects this specific input decides the message; a later check never runs. For a table or repeated inputs, trace EACH ROW separately — different rows are often rejected by different checks — and return one entry per row in `rows`.
   Verdicts: "consistent" only when every row's first rejection produces the quoted text (give `rows`); "contradicted" when an earlier check rejects some row first (say which row, which check, file:line, and the message it produces in `actual`); "unverifiable" when you could not read the entry point or a check on the way.

Never judge whether the change is correct, only whether the case's claim matches the code. Every file:line you give must be one you read in this run. Call report_recheck exactly once, with one verdict per item id."""

REPORT_RECHECK_TOOL = {
    "name": "report_recheck",
    "description": "Report, for every item id you were given, whether the code supports the case's claim.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["consistent", "contradicted", "unverifiable"]},
                        "traced_path": {"type": "string", "description": "The hops you read, file:line each."},
                        "actual": {"type": "string", "description": "For contradicted: what the user really sees (or what the old code really did), with the file:line that produces it."},
                        "reason": {"type": "string"},
                        "corrected_expected": {"type": "string", "description": "For contradicted error_path items: the full Expected for these steps, from the trace. Replaces the old one; never repeats the contradicted claim; names quoted whole, exactly as in the code."},
                        "steps_reach_intent": {"type": "boolean", "description": "For contradicted error_path items: whether these steps reach the condition the case's title is about."},
                        "actual_title": {"type": "string", "description": "When steps_reach_intent is false: a title for what these steps really test."},
                        "reachable_trigger": {
                            "type": "object",
                            "description": "When steps_reach_intent is false and the code shows a path a tester can drive that reaches the title's condition.",
                            "properties": {
                                "steps": {"type": "array", "items": {"type": "string"}},
                                "test_data": {"type": "string"},
                                "preconditions": {"type": "string"},
                                "expected": {"type": "string"},
                                "traced_path": {"type": "string", "description": "file:line per hop, files you read."},
                            },
                            "required": ["steps", "expected", "traced_path"],
                        },
                        "unreachable_reason": {"type": "string", "description": "When steps_reach_intent is false and no reachable trigger exists: why, and what unit test or review would check it."},
                        "rows": {
                            "type": "array",
                            "description": "Kind 3 only: one entry per input/row, traced from the entry point in execution order.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "input": {"type": "string"},
                                    "traced_path": {"type": "string", "description": "Entry point → each check passed → the check that rejects, file:line per hop."},
                                    "first_rejecting_check": {"type": "string", "description": "<file>:<line> of the check that rejects this input first."},
                                    "message": {"type": "string", "description": "The exact message (or status / error class) that check produces for this input."},
                                },
                                "required": ["input", "traced_path", "first_rejecting_check", "message"],
                            },
                        },
                    },
                    "required": ["id", "verdict", "reason"],
                },
            }
        },
        "required": ["verdicts"],
    },
}


def collect_trace_items(test_plan: Any, protected_titles: frozenset[str] = frozenset()) -> list[dict]:
    from .first_rejection import pinned, table_rows

    items: list[dict] = []
    cases = list(_cases(test_plan, GRADED_SECTIONS + ("security_negative_tests",)))
    pins = {id(c): pinned(c.get("expected") or "") for _, _, c in cases}
    # A case that pins exact error text is traced first: a wrong message fails a correct product.
    order = sorted(range(len(cases)), key=lambda k: (not pins[id(cases[k][2])], k))
    for section, i, case in (cases[k] for k in order):
        if case.get("covered_by_unit_test") or (section == "security_negative_tests" and not pins[id(case)]):
            continue
        text = " ".join([case.get("title") or "", case.get("expected") or "", " ".join(
            s for s in case.get("steps") or [] if isinstance(s, str))])
        if (pins[id(case)] or _FAILURE_RE.search(text)) and \
                len([x for x in items if x["kind"] == "error_path"]) < MAX_ITEMS:
            items.append({"id": f"{section}:{i}:q", "kind": "error_path", "section": section, "_case": case,
                          "protected": _protected(case, protected_titles),
                          "pins": [p["text"] for p in pins[id(case)]], "rows": table_rows(case)})
        before = [b for b in case.get("before_change") or [] if isinstance(b, dict)]
        claims = [b.get("claim") or "" for b in before] or [
            s for s in _sentences(case.get("expected") or "") if _BEFORE_RE.search(s)]
        if claims and len([x for x in items if x["kind"] == "before"]) < MAX_ITEMS // 2:
            items.append({"id": f"{section}:{i}:t", "kind": "before", "section": section, "_case": case,
                          "claims": claims[:3], "before_change": before[:3],
                          "protected": _protected(case, protected_titles)})
    return items


def build_trace_message(items: list[dict], prs: list[dict], bases: dict[str, dict]) -> str:
    from .diff_budget import allocate_patch_budget

    parts = ["## ITEMS"]
    for it in items:
        c = it["_case"]
        steps = "\n".join(f"  {n}. {s}" for n, s in enumerate(c.get("steps") or [], 1))
        head = (f"### {it['id']} — {it['kind']}\nTitle: {c.get('title')}\nPreconditions: "
                f"{c.get('preconditions') or '-'}\nSteps:\n{steps}\nExpected: {c.get('expected')}\n"
                f"Expected source (as the plan cites it): {c.get('expected_source') or '-'}\n"
                f"Traced path (as the plan states it): {c.get('traced_path') or '(none given)'}")
        if it.get("pins"):
            head += "\nPinned error text (check kind 3): " + "; ".join(repr(p) for p in it["pins"])
            head += f"\nTest data: {c.get('test_data') or '-'}"
            if it.get("rows"):
                head += f"\nTrace each of these {len(it['rows'])} rows separately."
        if it["kind"] == "before":
            head += "\nBefore-claims to check:\n" + "\n".join(f"  - {cl}" for cl in it["claims"])
            for b in it.get("before_change") or []:
                head += f"\n  (plan quotes old code {b.get('old_code')!r} from {b.get('source')})"
        parts.append(head)
    if bases:
        parts.append("## TICKET BASE (the code before this ticket's first PR)\n" + "\n".join(
            f"- {repo}: {b['sha'][:12]} (merge base of PR #{b['pr']}) — read it with read_file(at=\"base\")"
            for repo, b in bases.items()))
    parts.append("## PULL REQUESTS AND DIFFS (may be cut to fit)")
    per_pr = 24_000 // max(1, len(prs))
    for pr in prs:
        parts.append(f"- {pr.get('repository')} #{pr.get('number')}: {pr.get('title') or ''}")
        for name, patch, cut in allocate_patch_budget(pr.get("files_changed") or [],
                                                      total_budget=per_pr, per_file_cap=6000):
            parts.append(f"--- {pr.get('repository')}:{name}{' (cut)' if cut else ''}\n{patch}")
    return "\n\n".join(parts)


RETRACED_LABEL = "Re-traced from the code (rule Q):"
BEFORE_RETRACED_LABEL = "Before this change (rule T, re-read from the old code):"
NOT_REACHED_MARKER = "Unverified — these steps do not reach it (rule Q):"


def _note_replaced(case: dict, old: str, why: str) -> None:
    """The old claim lives on only in the grounding notes, never in the Expected a tester grades."""
    notes = case.setdefault("grounding_downgrades", [])
    if notes and notes[-1].get("claim", "").startswith("rule Q") and "original" not in notes[-1]:
        notes[-1]["original"] = old
    else:
        notes.append({"claim": f"rule Q: replaced Expected: {old}"[:200], "reason": why[:300], "original": old})


def _rewrite_contradicted(it: dict, v: dict, case: dict, why: str) -> None:
    """Rule Q, contradicted: the code-traced outcome REPLACES the Expected.

    The old sentence used to stay behind the marker, so the case asserted two
    incompatible outcomes (SK-3060 plan 721: "hasErrors is true … The failed
    step's request XML is still in the ZIP"). The old claim is kept on the
    case's ``trace_check`` and grounding notes only.
    """
    exp = case.get("expected") or ""
    corrected = (v.get("corrected_expected") or v.get("actual") or v.get("reason") or "").strip() \
        or "record what the steps really produce; the traced path shows a different result."
    outcome = (case.get("expected_outcome") or "").strip()
    if it.get("pins") and outcome:
        # Pinned text the trace contradicts: grade on the outcome the spec requires.
        from .first_rejection import OUTCOME_ONLY

        case["expected"] = f"{RETRACED_LABEL} {outcome} The code shows: {corrected} {OUTCOME_ONLY}"
    else:
        case["expected"] = f"{RETRACED_LABEL} {corrected}"
    case["trace_check"] = {"verdict": "contradicted", "actual": (v.get("actual") or why).strip(),
                           "traced_path": (v.get("traced_path") or "").strip(),
                           "corrected_expected": corrected, "original_expected": exp}


def _valid_trigger(t: Any) -> dict | None:
    if not isinstance(t, dict):
        return None
    steps = [s.strip() for s in t.get("steps") or [] if isinstance(s, str) and s.strip()]
    exp = (t.get("expected") or "").strip()
    if not steps or not exp or not _FILE_LINE_RE.search(t.get("traced_path") or ""):
        return None  # a trigger the critic did not trace is not a trigger
    return {**t, "steps": steps, "expected": exp}


def _refit_to_intent(it: dict, v: dict, case: dict, report: dict, test_plan: Any) -> None:
    """The corrected trace shows the steps never reach the condition the title is about.

    With a traced, reachable trigger the case keeps its title and gets the
    steps that reach it, and what the old steps really produce becomes its own
    case. Without one the case is retitled to what its steps produce, the
    unreachable detail is marked unverified (never asserted), and the intent is
    listed for code review / a unit test (``not_gradeable``, which
    ``route_out_of_uat`` moves).
    """
    import copy

    section = it["section"]
    title = (case.get("title") or "").strip()
    actual_title = (v.get("actual_title") or "").strip() or f"What these steps produce (they do not reach: {title})"
    items = getattr(test_plan, section, None) if test_plan is not None else None
    trig = _valid_trigger(v.get("reachable_trigger"))
    if trig and isinstance(items, list):
        as_written = copy.deepcopy(case)
        as_written["title"] = actual_title
        as_written["split_from"] = title
        case["steps"] = trig["steps"]
        for k in ("test_data", "preconditions"):
            if (trig.get(k) or "").strip():
                case[k] = trig[k].strip()
        case["expected"] = f"{RETRACED_LABEL} {trig['expected']}"
        case["traced_path"] = trig["traced_path"].strip()
        case["trace_check"]["corrected_expected"] = trig["expected"]
        case["trace_check"]["refit"] = "steps replaced with a traced path that reaches the title's condition"
        items.append(as_written)
        _record(report, case, section, "Q", "downgraded",
                "the steps never reached the condition the title is about; they now follow a traced path that "
                f"does, and what the old steps produce is its own case (\"{actual_title[:80]}\")", title)
        return
    reason = (v.get("unreachable_reason") or "").strip() or \
        "the re-check found no path a tester can drive that reaches it; check it by code review or a unit test"
    case["title"] = actual_title
    case["intent_not_reached"] = {"intent": title, "reason": reason}
    case["expected"] = f"{case['expected']} {NOT_REACHED_MARKER} {title} — {reason}"
    if isinstance(items, list):
        items.append({
            "title": title, "steps": [], "expected": title,
            "expected_source": case.get("expected_source") or "", "grounded_in": list(case.get("grounded_in") or []),
            "traced_path": (v.get("traced_path") or "").strip(), "expected_verified": False,
            "split_from": actual_title, "suggested_test": reason,
            "not_gradeable": {"reason": f"No path a tester can drive reaches this (rule Q): {reason}"},
        })
    _record(report, case, section, "Q", "moved",
            f"the steps never reach the condition the title is about ({reason}); the case now asserts only what "
            "its steps produce and the intent is listed for code review", title)


def _rewrite_before(it: dict, case: dict, why: str) -> bool:
    """Rule T, contradicted: replace the before-claim sentences, never keep both."""
    exp = case.get("expected") or ""
    claims = [_norm(c) for c in it.get("claims") or [] if c]
    out, replaced = [], False
    for s in _sentences(exp):
        if any(c and (c == _norm(s) or c in _norm(s)) for c in claims):
            if not replaced:
                out.append(f"{BEFORE_RETRACED_LABEL} {why}")
                replaced = True
        else:
            out.append(s)
    if replaced:
        case["expected"] = " ".join(out)
    return replaced


def apply_trace_verdicts(items: list[dict], verdicts: dict[str, dict], report: dict,
                         test_plan: Any = None) -> dict:
    changes = {"contradicted": [], "consistent": [], "unverifiable": []}
    for it in items:
        v = verdicts.get(it["id"])
        case = it["_case"]
        if not v:
            changes["unverifiable"].append(it["id"])
            continue
        changes[v["verdict"]].append(it["id"])
        if v["verdict"] != "contradicted":
            if v["verdict"] == "consistent" and it["kind"] == "error_path" and not (case.get("traced_path") or "").strip() \
                    and (v.get("traced_path") or "").strip():
                case["traced_path"] = v["traced_path"].strip()
            rows = [r for r in v.get("rows") or [] if isinstance(r, dict)]
            if v["verdict"] == "consistent" and it.get("pins") and rows and not case.get("rejection_traces"):
                # Per-row traces the critic read stand in for the ones the generator omitted;
                # the save-time check still re-reads each against the code.
                case["rejection_traces"] = [{k: r.get(k) for k in ("input", "traced_path",
                                                                    "first_rejecting_check", "message")}
                                            for r in rows]
            continue
        # Never cut the critic's text: a 300-char slice cut a step file name
        # in half in a tester's Expected (SK-3060 "step2_GetCustomerConfiguredPr").
        why = (v.get("actual") or v.get("reason") or "").strip() or "the traced path shows a different result"
        exp = case.get("expected") or ""
        if it["kind"] == "error_path":
            if "(rule Q)" in exp:
                continue
            _rewrite_contradicted(it, v, case, why)
            case["expected_verified"] = False
            _record(report, case, it["section"], "Q", "downgraded",
                    f"expected result contradicts the traced error path; replaced with the traced outcome: "
                    f"{why[:300]}", case.get("title") or "")
            _note_replaced(case, exp, why)
            if v.get("steps_reach_intent") is False and not it.get("protected"):
                _refit_to_intent(it, v, case, report, test_plan)
        else:
            if "(rule T)" not in exp and not _rewrite_before(it, case, why):
                case["expected"] = f"{BEFORE_MARKER.format(why=why[:300])} {exp}".strip()
            case["before_check"] = {"verdict": "contradicted", "actual": why, "original_expected": exp}
            _record(report, case, it["section"], "T", "downgraded",
                    f"before-this-change claim contradicts the old code: {why[:300]}", case.get("title") or "")
    return changes


async def run_trace_critic(llm: Any, github: Any, test_plan: Any, dev_infos: list, report: dict, *,
                           bases: dict[str, dict] | None = None,
                           protected_titles: frozenset[str] = frozenset()) -> str | None:
    """Rules Q and T. Returns a gap sentence when the critic could not run."""
    from .code_exploration import Workspace, _new_result, run_tool_loop

    items = collect_trace_items(test_plan, protected_titles)
    if not items:
        return None
    prs = [pr for dev in dev_infos for pr in ((dev or {}).get("pull_requests") or [])
           if isinstance(pr, dict) and pr.get("repository")]
    if not prs or not hasattr(llm, "_post_messages"):
        return None
    result = _new_result()
    ws = Workspace(github, prs, max_searches=MAX_SEARCHES, max_reads=MAX_READS,
                   base_refs={r: b["sha"] for r, b in (bases or {}).items() if b and b.get("sha")})
    what = f"{len(items)} failure-path or before-behaviour claim(s) were not re-traced against the code"
    try:
        raw = await run_tool_loop(llm, ws, system=TRACE_SYSTEM_PROMPT,
                                  first_message=build_trace_message(items, prs, bases or {}),
                                  report_tool=REPORT_RECHECK_TOOL, max_turns=MAX_TURNS, result=result)
    except Exception as e:  # never fails the plan; the gap says what was skipped
        logger.warning("trace critic failed: %s", e, exc_info=True)
        return f"the error-path re-check could not run ({type(e).__name__}), so {what}"
    if raw is None:
        return f"the error-path re-check {result.get('error') or 'did not report'}, so {what}"
    verdicts = {}
    for v in (raw or {}).get("verdicts") or []:
        if isinstance(v, dict) and v.get("verdict") in ("consistent", "contradicted", "unverifiable"):
            # A contradiction must rest on a file the critic read in this run.
            if v["verdict"] == "contradicted" and not _cites_read_file(v, ws):
                v = {**v, "verdict": "unverifiable",
                     "reason": "contradiction cites no file read in this run: " + (v.get("reason") or "")}
            verdicts[str(v.get("id"))] = v
    changes = apply_trace_verdicts(items, verdicts, report, test_plan)
    report["trace"] = {**changes, "usage": result.get("usage"), "reads": ws.reads, "searches": ws.searches}
    return None


def _cites_read_file(v: dict, ws) -> bool:
    text = " ".join([v.get("actual") or "", v.get("traced_path") or "", v.get("reason") or ""])
    read = {posixpath.basename(p) for _, p in getattr(ws, "read_files", set())}
    return any(posixpath.basename(m.group(1)) in read for m in _PATH_RE.finditer(text))


# ---------------------------------------------------------------------------
# Q, save time. A correction never sits beside the claim it corrects, and a
# name a tester is told to look for is the code's name, whole.
# ---------------------------------------------------------------------------

_LEGACY_Q_RE = re.compile(r"Contradicted by the code \(rule Q\):\s*(?P<why>.*?)\s+—\s+(?P<rest>.*)\Z", re.S)
_ORIGINAL_RE = re.compile(r"\s*\(Original expected:.*\)\s*\Z", re.S)


def _strip_contradicted(case: dict) -> tuple[str, str] | None:
    """``(new_expected, removed)`` when the Expected keeps a contradicted claim; None when it does not."""
    exp = case.get("expected") or ""
    tc = case.get("trace_check") if isinstance(case.get("trace_check"), dict) else {}
    if "Contradicted by the code" in exp:
        # The old format: "<marker> <why> — <the contradicted Expected>", or the
        # pinned form with "(Original expected: …)". Also what a generator copies
        # from a previous plan shown in a regeneration prompt.
        actual = (tc.get("corrected_expected") or tc.get("actual") or "").strip()
        head, rest = exp, ""
        if actual:
            for cut in (300, len(actual)):
                lead = f"{CONTRADICTED_MARKER.format(why=actual[:cut])} "
                if exp.startswith(lead):
                    head, rest = lead, exp[len(lead):]
                    break
        if not rest:
            m = _LEGACY_Q_RE.search(exp)
            if not m:
                return None
            actual = actual or m.group("why").strip()
            head, rest = exp[:m.start("rest")], m.group("rest")
        keep = ""
        if _ORIGINAL_RE.search(rest):  # pinned form: the outcome stays, the original goes
            keep = _ORIGINAL_RE.sub("", rest).strip()
            removed = _ORIGINAL_RE.search(rest).group(0).strip()[len("(Original expected:"):-1].strip()
        else:
            removed = rest.strip()
        before = exp[:exp.index("Contradicted by the code")].strip()
        if len(actual) == 300 and not tc.get("corrected_expected"):
            actual += "…"  # the old 300-char slice; show it was cut, not that it ends there
        elif actual and actual[-1] not in ".!?…)":
            actual += "."
        new = " ".join(x for x in (before, RETRACED_LABEL, actual, keep) if x).strip()
        return new, removed
    original = (tc.get("original_expected") or "").strip()
    if tc.get("verdict") == "contradicted" and original and RETRACED_LABEL in exp:
        corrected = _norm(tc.get("corrected_expected") or "")
        stale = [n for n in (_norm(x) for x in _sentences(original)) if n and n not in corrected]
        sents = _sentences(exp)
        # A stale sentence, or a piece of one, that the correction itself does not say.
        drop = [x for x in sents if (n := _norm(x)) and n not in corrected
                and any(n == st or (len(n) >= 20 and n in st) for st in stale)]
        if drop:
            return " ".join(x for x in sents if x not in drop), " ".join(drop)
    return None


def check_contradicted_expected(test_plan: Any, report: dict) -> int:
    """Reject an Expected that carries both a rule Q correction and the claim it contradicts."""
    n = 0
    for section, _, case in _cases(test_plan, GRADED_SECTIONS + ("security_negative_tests",)):
        got = _strip_contradicted(case)
        if not got:
            continue
        new, removed = got
        case["expected"] = new
        case["expected_verified"] = False
        tc = case.setdefault("trace_check", {"verdict": "contradicted"})
        tc.setdefault("original_expected", removed)
        n += 1
        _record(report, case, section, "Q", "downgraded",
                "Expected kept the claim the code contradicts beside its correction; the contradicted "
                "sentence(s) were removed and are kept in the grounding notes only", case.get("title") or "")
        _note_replaced(case, removed, "contradicted by the code (rule Q)")
    return n


# A file or step name: joined by "_", or ending in a file extension.
_NAME_TOKEN_RE = re.compile(r"(?<![\w/.-])[A-Za-z][A-Za-z0-9]*(?:(?:_|\.)[A-Za-z0-9]+)+")
_FILE_EXT_RE = re.compile(r"\.(?:xml|json|txt|csv|zip|pdf|html?|ya?ml|log|ts|tsx|js|jsx|py|kt|swift|java|cs|rb|go)$", re.I)
_CODE_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_IDENT_SEG_RE = re.compile(r"^(?=.{8,}$)(?:[a-z0-9]+[A-Z]|[A-Z][a-z0-9]+[A-Z])[A-Za-z0-9]*$")


def _code_tokens(index) -> set[str] | None:
    files = getattr(index, "files", None)
    if not isinstance(files, dict) or not files:
        return None
    toks: set[str] = set()
    for src in files.values():
        for t in _CODE_TOKEN_RE.findall(getattr(src, "text", "") or ""):
            toks.add(t)
            toks.update(p for p in t.split("_") if p)
    for paths in (getattr(index, "trees", None) or {}).values():
        for path in paths or []:
            for t in _CODE_TOKEN_RE.findall(posixpath.basename(path)):
                toks.add(t)
                toks.update(p for p in t.split("_") if p)
    return toks


def check_quoted_names(test_plan: Any, report: dict, index=None) -> int:
    """A file or step name quoted in an Expected must be in the code whole.

    Only a name that is a strict prefix of exactly one code token, or of one
    base name the other matches extend (a cut name, like SK-3060's
    ``step2_GetCustomerConfiguredPr``), is fixed; a prefix of several is marked; one with no match is counted as unchecked, because the
    source index holds only the PRs' files ("not read" is never "not in the code").
    """
    tokens = _code_tokens(index)
    n, unchecked = 0, []
    for section, _, case in _cases(test_plan, GRADED_SECTIONS + ("security_negative_tests",)):
        exp = case.get("expected") or ""
        segs = {seg for tok in _NAME_TOKEN_RE.findall(exp) if "_" in tok or _FILE_EXT_RE.search(tok)
                for seg in re.split(r"[_.]", tok) if _IDENT_SEG_RE.match(seg)}
        if not segs:
            continue
        if tokens is None:
            unchecked.append(case.get("title") or section)
            continue
        for seg in sorted(segs):
            if seg in tokens:
                continue
            longer = sorted(t for t in tokens if t.startswith(seg) and len(t) > len(seg) and "_" not in t)
            # GetCustomerConfiguredPropertyStates{,Result,Response}: the base name is the one cut.
            base = min(longer, key=len) if longer else ""
            if base and all(t.startswith(base) for t in longer):
                longer = [base]
            pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(seg)}(?![A-Za-z0-9])")
            if len(longer) == 1:
                case["expected"] = pat.sub(longer[0], case["expected"])
                n += 1
                _record(report, case, section, "Q", "fixed",
                        f"Expected quoted a cut name \"{seg}\"; the code's name is \"{longer[0]}\"", seg)
            elif longer:
                case["expected"] = pat.sub(
                    f"{seg} (Unverified — not a whole name in the code; it has {', '.join(longer[:3])})",
                    case["expected"], count=1)
                case["expected_verified"] = False
                n += 1
                _record(report, case, section, "Q", "marked",
                        f"Expected quotes \"{seg}\", which is not a whole name in the code "
                        f"(it has {', '.join(longer[:3])})", seg)
            else:
                unchecked.append(f"{case.get('title') or section}: {seg}")
    if unchecked:
        report.setdefault("unchecked", []).extend(
            f"rule Q: quoted name not in the PRs' files (could not check) — {u}"[:200] for u in unchecked[:10])
    return n


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

async def recheck_before_routing(llm: Any, github: Any, test_plan: Any, dev_infos: list, report: dict, *,
                                 index=None, exploration: dict | None = None,
                                 bases: dict[str, dict] | None = None,
                                 protected_titles: frozenset[str] = frozenset()) -> list[str]:
    """Rules U, Q, T, then AG, AE, AC, AA, AB. Returns gap sentences for checks that could not run."""
    gaps = []
    try:
        report["scope"] = await check_graded_scope(test_plan, dev_infos, report, index=index, github=github,
                                                   exploration=exploration, protected_titles=protected_titles)
    except Exception as e:
        logger.warning("rule U check failed: %s", e, exc_info=True)
        gaps.append(f"the graded-scope re-check could not run ({type(e).__name__})")
    gap = await run_trace_critic(llm, github, test_plan, dev_infos, report, bases=bases,
                                 protected_titles=protected_titles)
    if gap:
        gaps.append(gap)
    # Rules AA–AG that move or rewrite cases (src/app/deployed_env_checks.py):
    # after the trace critic, so its marks land on the generator's text.
    from . import deployed_env_checks

    try:
        await deployed_env_checks.recheck_before_routing(test_plan, report, index=index, github=github,
                                                         protected_titles=protected_titles)
    except Exception as e:
        logger.warning("rules AA-AG check failed: %s", e, exc_info=True)
        gaps.append(f"the deployed-environment re-check (rules AA–AG) could not run ({type(e).__name__})")
    if gaps:
        report.setdefault("not_run", []).extend(gaps)
    return gaps


def recheck_before_save(test_plan: Any, report: dict, *, repo_evidence: list[dict] | None = None,
                        index=None) -> None:
    """Rules Q (no correction beside its claim; whole names), S, R, Q (first rejecting check), X, Y,
    AD–AG, Z — code only, marks in place."""
    from .build_commands import check_commands
    from .first_rejection import check_pinned_messages

    check_contradicted_expected(test_plan, report)
    check_quoted_names(test_plan, report, index)
    check_test_claims(test_plan, report)
    check_signal_claims(test_plan, report, index)
    check_pinned_messages(test_plan, report, index)
    check_runnable(test_plan, report)
    for f in check_commands(test_plan, repo_evidence):
        report.setdefault("findings", []).append({
            "section": f["case_id"].split(":")[0], "title": _title_at(test_plan, f["case_id"]),
            "rule": "Y", "action": "marked", "reason": f"{f['where']}: {f['reason']}"})
    if not any(isinstance(e, dict) and e.get("commands") for e in repo_evidence or []):
        report.setdefault("not_run", []).append(
            "commands and identifiers were not checked against the build config (it was not read)")
    # Rules AD, AE, AF, AG (src/app/deployed_env_checks.py). Before the hoist,
    # so a "blocked by setup" mark is never folded into a shared precondition.
    from . import deployed_env_checks

    deployed_env_checks.recheck_before_save(test_plan, report, repo_evidence=repo_evidence)
    hoist_shared_preconditions(test_plan, report)


def _title_at(test_plan: Any, case_id: str) -> str:
    section, _, idx = case_id.partition(":")
    items = getattr(test_plan, section, None) or []
    try:
        item = items[int(idx)]
    except (ValueError, IndexError):
        return ""
    return (item.get("title") if isinstance(item, dict) else str(item))[:140]


def summarize(report: dict) -> dict:
    """Counts for the log and the re-read line."""
    findings = report.get("findings") or []
    return {
        "downgraded": sum(1 for f in findings if f["action"] in ("downgraded", "marked")),
        "moved": sum(1 for f in findings if f["action"] == "moved"),
        "fixed": sum(1 for f in findings if f["action"] == "fixed"),
    }
