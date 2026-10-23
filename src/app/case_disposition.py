"""What is a runnable UAT case, and where the rest of the plan's findings go.

A case in a numbered section is counted, graded and marked by a tester: it
enters the progress key, the pass/fail totals and the UAT runner's id space.
Three kinds of finding kept arriving there that a tester cannot grade, and
each time the plan already knew it:

* **Unreachable.** SK-2889's ``edge_cases:0`` and ``edge_cases:4`` said, in
  their own expected results, that nothing a tester does produces the
  condition (rule L). They stayed runnable cases, so a tester had to mark
  something that could neither pass nor fail. These belong under "verify by
  code review / unit test", each with the test that would prove it.
* **Pre-existing.** SK-2889's ``edge_cases:3`` was labelled "Pre-existing
  behaviour, not changed by this ticket" (rule H) and still counted toward the
  ticket's pass/fail total. It is a note about the code around the change.
* **Unanchored regression lines.** SK-2889's twelve regression items were all
  marked "Unverified — assumption". A regression line is only worth a
  tester's time when it names the changed file or call site that puts the
  area at risk; a line that cites none is a guess, kept visible as
  low-confidence and out of the core checklist.

``route_out_of_uat`` moves each of these into its own top-level list. The
lists sit beside ``needs_spec_cases`` and, like it, are not in
``progress_key.CHECKLIST_KEYS``, so they are outside the case count, the
progress key and the runner's case ids. Plans stored before this change keep
those cases in place and therefore keep the key they were recorded under;
only plans generated after it are shaped this way (see docs/FEATURES.md,
"Plan shape v2").

Cases QA already passed in the previous version (``protected_titles``) are
never moved: a case someone graded was gradeable, whatever a critic says now,
and moving it would shift every recorded id after it.
"""

from __future__ import annotations

import logging
import posixpath
import re
from typing import Any, Iterable

from .build_targets import cited_paths, path_matches

logger = logging.getLogger(__name__)

#: Written on every plan body generated with these lists. A body without it is
#: shape 1: its unreachable and pre-existing cases are still in the numbered
#: sections, counted and keyed as they were recorded.
PLAN_SHAPE = 2

CODE_REVIEW_KEY = "code_review_checks"
PRE_EXISTING_KEY = "pre_existing_behaviour"
LOW_CONFIDENCE_KEY = "regression_low_confidence"
#: Every list ``route_out_of_uat`` fills. None of them is a checklist section.
OUT_OF_UAT_KEYS = (CODE_REVIEW_KEY, PRE_EXISTING_KEY, LOW_CONFIDENCE_KEY)

CASE_SECTIONS = ("happy_path", "edge_cases", "integration_tests", "security_negative_tests")

PRE_EXISTING_LABEL = "Pre-existing behaviour, not changed by this ticket:"
_PRE_EXISTING_RE = re.compile(r"^\s*(?:[🔴🟡🟢]\s*)?(?:[^:]{0,80}:\s*)?Pre-existing behaviou?r\b", re.I)
_UNREACHABLE_RE = re.compile(r"Not reachable in the running app\b|\bcannot be (?:produced|triggered) "
                             r"(?:from|through|in) the (?:running )?(?:app|UI|product)\b", re.I)
_MARKER_PREFIX_RE = re.compile(
    r"^(?:Not reachable in the running app[^:]*\(rule L\):\s*|Pre-existing behaviou?r, not changed by this ticket:\s*)",
    re.I,
)


def _protected(case: dict, protected_titles: frozenset[str]) -> bool:
    if not protected_titles:
        return False
    from .services.plan_revision import CONFIDENT, _score, protected_title

    title = protected_title(case.get("title") or "")
    return any(_score(title, p) >= CONFIDENT for p in protected_titles)


def is_unreachable(case: dict) -> bool:
    """The plan concluded no tester can produce this case's condition.

    From the reachability critic's record, the generator's own ``reachable:
    false`` (rule L), or the rule-L wording in its expected result. A case
    converted to proven unit coverage is not "unreachable" here: it already
    lives in the covered namespace with the test that proves it.
    """
    if not isinstance(case, dict) or case.get("covered_by_unit_test"):
        return False
    r = case.get("reachability")
    if isinstance(r, dict) and r.get("reachable") is False:
        return True
    if case.get("reachable") is False:
        return True
    return bool(_UNREACHABLE_RE.search(case.get("expected") or ""))


def is_pre_existing(case: dict) -> bool:
    if not isinstance(case, dict):
        return False
    return case.get("pre_existing") is True or bool(_PRE_EXISTING_RE.match(case.get("expected") or ""))


def _code_refs(case: dict) -> list[str]:
    refs = []
    src = case.get("expected_source")
    if isinstance(src, str) and src.strip():
        refs.append(src.strip())
    for g in case.get("grounded_in") or []:
        if isinstance(g, str) and g.startswith("pr:") and g[3:] not in " ".join(refs):
            refs.append(g[3:])
    return refs[:4]


def _strip_marker(text: str) -> str:
    text = (text or "").strip()
    prev = None
    while prev != text:
        prev = text
        text = _MARKER_PREFIX_RE.sub("", text).strip()
    return text


def suggested_test(case: dict) -> str:
    """The unit test that would prove what the case could not, as concretely
    as the case allows: the critic's or generator's suggestion first."""
    r = case.get("reachability") if isinstance(case.get("reachability"), dict) else {}
    for s in (case.get("suggested_test"), r.get("suggested_test")):
        if isinstance(s, str) and s.strip():
            return s.strip()
    if r.get("unit_test_ref"):
        return (f"{r['unit_test_ref']} asserts this branch; confirm in review that it covers the "
                "condition below, and that CI runs it.")
    refs = _code_refs(case)
    where = f" for {refs[0]}" if refs else ""
    what = _strip_marker(r.get("original_expected") or case.get("expected") or "")
    what = what.split(" — ", 1)[-1] if " — " in what[:400] else what
    return (f"Add a unit test{where} that builds the condition directly (the input or state the "
            f"app cannot produce) and asserts: {what[:300].rstrip()}")


def _code_review_entry(case: dict, section: str) -> dict:
    r = case.get("reachability") if isinstance(case.get("reachability"), dict) else {}
    why = (r.get("reason") or case.get("unreachable_reason") or "").strip()
    if not why:
        m = _UNREACHABLE_RE.search(case.get("expected") or "")
        why = _strip_marker((case.get("expected") or "")[m.start():] if m else "").split(" — ", 1)[0]
    entry = {
        "title": r.get("original_title") or case.get("title") or "",
        "origin_section": section,
        "why_not_uat": why or "The plan found nothing in the product that produces this condition.",
        "what_to_verify": _strip_marker(r.get("original_expected") or case.get("expected") or ""),
        "suggested_test": suggested_test(case),
        "code_refs": _code_refs(case),
    }
    for k in ("owner", "suggested_ticket"):
        if r.get(k):
            entry[k] = r[k]
    return entry


def _pre_existing_entry(case: dict, section: str) -> dict:
    return {
        "title": case.get("title") or "",
        "origin_section": section,
        "note": _strip_marker(case.get("expected") or ""),
        "code_refs": _code_refs(case),
    }


# ---------------------------------------------------------------------------
# Regression lines: each names the changed code that puts its area at risk
# ---------------------------------------------------------------------------

def regression_anchor(line: str, changed: Iterable[str], call_sites: Iterable[str] = (),
                      patches: str = "") -> str | None:
    """The changed file or call site a regression line cites, or None.

    A citation counts when a path in the line names a file the PRs changed, a
    file the code explorer found calling changed code, or a module the changed
    code itself imports or calls (its name appears in the diff as a module
    reference: ``require('./http')``, ``import Foo``, ``from .http import``).
    """
    changed, call_sites = list(changed), list(call_sites)
    # A type named after a changed file (`WebKitViewController` for
    # WebKitViewController.swift) cites it as surely as its path does.
    stems = {posixpath.splitext(posixpath.basename(c))[0]: c for c in changed}
    for word in re.findall(r"\b[A-Z][A-Za-z0-9]{5,}\b", line or ""):
        if word in stems and re.search(r"[a-z][A-Z]", word):
            return stems[word]
    for p in cited_paths(line):
        if any(path_matches(p, c) for c in changed):
            return p
        if any(path_matches(p, c) for c in call_sites):
            return p
        stem = posixpath.splitext(posixpath.basename(p))[0]
        if patches and len(stem) >= 3 and re.search(
                rf"(?:[./'\"]|\bimport\s+|\bfrom\s+[\w.]*){re.escape(stem)}\b", patches):
            return p
    return None


def _call_sites(exploration: dict | None) -> list[str]:
    return [f.get("path") for f in (exploration or {}).get("findings") or []
            if isinstance(f, dict) and f.get("path")]


def changed_paths(dev_infos: Iterable[dict | None]) -> list[str]:
    return sorted({f.get("filename") for dev in dev_infos for pr in ((dev or {}).get("pull_requests") or [])
                   if isinstance(pr, dict) for f in (pr.get("files_changed") or [])
                   if isinstance(f, dict) and f.get("filename")})


# ---------------------------------------------------------------------------

def route_out_of_uat(test_plan: Any, *, dev_infos: Iterable[dict | None] = (),
                     exploration: dict | None = None,
                     protected_titles: frozenset[str] = frozenset()) -> dict[str, list]:
    """Move every case and line a tester cannot grade out of the numbered sections.

    Returns ``{CODE_REVIEW_KEY: [...], PRE_EXISTING_KEY: [...],
    LOW_CONFIDENCE_KEY: [...], "moved": [ids]}``; the lists also include any
    entries the generator wrote there itself (``code_review_checks``). Runs
    after the reachability critic and before the evidence checks, so a moved
    case gets no deploy check and the critics that count cases never see it.
    A regression line is checked for an anchor only when the PRs' changed
    files are known: with no file list there is nothing to check against, and
    "could not check" never demotes a line.
    """
    dev_infos = list(dev_infos)
    out: dict[str, list] = {CODE_REVIEW_KEY: [], PRE_EXISTING_KEY: [], LOW_CONFIDENCE_KEY: [], "moved": []}
    for item in getattr(test_plan, CODE_REVIEW_KEY, None) or []:
        if isinstance(item, dict) and (item.get("title") or "").strip():
            out[CODE_REVIEW_KEY].append({
                "title": item["title"].strip(), "origin_section": "generator",
                "why_not_uat": (item.get("why_not_uat") or "").strip(),
                "what_to_verify": (item.get("what_to_verify") or "").strip(),
                "suggested_test": (item.get("suggested_test") or "").strip()
                or "Add a unit test that drives this branch directly.",
                "code_refs": [str(r) for r in item.get("code_refs") or [] if r][:4],
            })
    for section in CASE_SECTIONS:
        items = getattr(test_plan, section, None)
        if not isinstance(items, list):
            continue
        keep = []
        for i, case in enumerate(items):
            if not isinstance(case, dict) or _protected(case, protected_titles):
                keep.append(case)
            elif is_unreachable(case):
                out[CODE_REVIEW_KEY].append(_code_review_entry(case, section))
                out["moved"].append(f"{section}:{i}")
            elif is_pre_existing(case):
                out[PRE_EXISTING_KEY].append(_pre_existing_entry(case, section))
                out["moved"].append(f"{section}:{i}")
            else:
                keep.append(case)
        setattr(test_plan, section, keep)

    lines = getattr(test_plan, "regression_checklist", None)
    if isinstance(lines, list):
        changed = changed_paths(dev_infos)
        sites = _call_sites(exploration)
        patches = "\n".join(f.get("patch") or "" for dev in dev_infos
                            for pr in ((dev or {}).get("pull_requests") or []) if isinstance(pr, dict)
                            for f in (pr.get("files_changed") or []) if isinstance(f, dict))
        keep = []
        for i, line in enumerate(lines):
            if not isinstance(line, str) or _protected({"title": line}, protected_titles):
                keep.append(line)
            elif _PRE_EXISTING_RE.match(line):
                out[PRE_EXISTING_KEY].append({"title": line, "origin_section": "regression_checklist",
                                              "note": line, "code_refs": cited_paths(line)[:4]})
                out["moved"].append(f"regression_checklist:{i}")
            elif changed and not regression_anchor(line, changed, sites, patches):
                out[LOW_CONFIDENCE_KEY].append({
                    "line": line,
                    "why": "cites no file the change touched and no call site of changed code, so "
                           "nothing shows the change puts this area at risk",
                })
                out["moved"].append(f"regression_checklist:{i}")
            else:
                keep.append(line)
        test_plan.regression_checklist = keep
    if out["moved"]:
        logger.info("moved out of UAT: %s", out["moved"])
    return out


# ---------------------------------------------------------------------------
# Rendering for the CLI and the MCP server (the UI and Jira render in
# frontend/src/utils/markdown.js, with the same wording)
# ---------------------------------------------------------------------------

CODE_REVIEW_BLURB = ("Not UAT cases. The plan found that nothing a tester does in the product produces these "
                     "conditions, so they cannot pass or fail in UAT. Verify each in code review or with the "
                     "suggested unit test.")
PRE_EXISTING_BLURB = ("Behaviour that existed before this change and that it did not touch. Shown for context; "
                      "a result here is not a pass or fail for this ticket.")
LOW_CONFIDENCE_BLURB = ("Regression ideas that cite no file the change touched and no call site of changed "
                        "code. Kept out of the core checklist; check one only if you know why it is at risk.")


def render_out_of_uat_lines(plan: dict, markdown: bool = True) -> list[str]:
    """The three shape-v2 lists, after the numbered sections. Empty for shape 1."""
    out: list[str] = []

    def head(md: str, text: str, blurb: str):
        if markdown:
            out.extend([md, "", f"_{blurb}_", ""])
        else:
            out.extend(["", text, "-" * 60, "", blurb, ""])

    review = plan.get(CODE_REVIEW_KEY) or []
    if review:
        head(f"## 🔍 Verify by Code Review / Unit Test ({len(review)})",
             f"VERIFY BY CODE REVIEW / UNIT TEST ({len(review)})", CODE_REVIEW_BLURB)
        for i, c in enumerate(review, 1):
            out.append(f"{i}. {'**' if markdown else ''}{c.get('title')}{'**' if markdown else ''}")
            for label, key in (("Why not UAT", "why_not_uat"), ("Verify", "what_to_verify"),
                               ("Suggested test", "suggested_test")):
                if c.get(key):
                    out.append(f"   {'- ' if markdown else ''}{label}: {c[key]}")
            if c.get("code_refs"):
                out.append(f"   {'- ' if markdown else ''}Code: {', '.join(c['code_refs'])}")
        out.append("")
    notes = plan.get(PRE_EXISTING_KEY) or []
    if notes:
        head(f"## 📎 Pre-existing Behaviour — Notes ({len(notes)})",
             f"PRE-EXISTING BEHAVIOUR — NOTES ({len(notes)})", PRE_EXISTING_BLURB)
        for i, c in enumerate(notes, 1):
            out.append(f"{i}. {c.get('title')}")
            if c.get("note") and c.get("note") != c.get("title"):
                out.append(f"   {'- ' if markdown else ''}{c['note']}")
        out.append("")
    low = plan.get(LOW_CONFIDENCE_KEY) or []
    if low:
        head(f"## 🌫️ Low-confidence Regression Ideas ({len(low)})",
             f"LOW-CONFIDENCE REGRESSION IDEAS ({len(low)})", LOW_CONFIDENCE_BLURB)
        out.extend(f"- {c.get('line')}" for c in low)
        out.append("")
    return out
