"""Find test steps that name a UI control, action or flag no code backs.

The generator turns ticket wording into concrete steps. When the ticket says
"re-link the calculation" the model writes "Link the same calculation to the
same Forms file again using Connect" — a literal control, in a literal state —
and nothing downstream asks whether the app offers that control in that state.
SK-2579 (plan 610, edge_cases:0, step 6) is the case this was written for: in
the mobile app a linked file shows only Disconnect, so the step cannot be
performed and a correct build failed UAT. The same plan put an ``autoSync``
flag in its preconditions that exists only in the ticket text. Every case in
it was grounded in ``ticket:SK-2579-desc`` and nothing else, because all 20
linked PRs were unreadable that morning.

The existing guards could not see it. ``needs_manual_verification`` is set by
the model, and the model did not set it; the critics key off AC text, not
step text; ``expected_verified: false`` badged the expected result and said
nothing about the step that had to run first.

What counts as backing
----------------------
Code, and only code: a ``pr:<path>`` that names a file the linked PRs actually
changed, or a ``testid:<screen>`` from the testID reference. ``ticket:``,
``spec:`` and AC ids record what someone *wanted*; they cannot show that a
control ships, which is the whole failure. A ``pr:`` citation is checked
against the files that were readable, so a path from a PR GitHub refused, or
one the model made up, does not count.

Where references come from
--------------------------
1. ``step_grounding`` — the model's own declaration, one entry per control,
   action, route or flag a step names, with the code source it rests on.
2. A lexical backstop for the references the model did not declare: code
   identifiers (``autoSync``, ``formsDocument.autoSync``), a named control
   after a UI verb ("using Connect", "tap 'Save'"), a named control before a
   control noun ("the Leave PDF option"). An undeclared reference has no
   source of its own, so it is backed only when the case cites code at all.

What happens to an unbacked reference
-------------------------------------
* The step (or the preconditions) is prefixed in place with an
  ``Unverified — assumption`` marker, so it travels into every renderer and
  into Jira, and the reference is listed on the case as ``unverified_steps``.
* A ``grounding_warnings`` entry names it.
* When it sits in a step (not the preconditions), the case is marked
  ``needs_manual_verification`` — the flag the model should have set. If the
  expected result is unverified too, the existing quarantine then moves the
  case to ``needs_spec_cases``: neither what the tester does nor what they
  should see traces to code. A precondition-only reference is a warning: it
  is setup, and the behaviour under test may still be gradeable.

The step count never changes and no step is removed, so the progress-key
fingerprint is unaffected by the marking itself. This module is pure — no
network, no repository — and runs after the critics, before quarantine.
"""

from __future__ import annotations

import re

#: Sections whose cases this pass checks. Same set quarantine works on; see
#: ``_QUARANTINABLE_SECTIONS`` for why security negative cases are excluded.
CHECKED_SECTIONS = ("happy_path", "edge_cases", "integration_tests")

#: ``grounded_in`` / ``step_grounding`` source kinds that are code.
CODE_KINDS = ("pr", "testid")

#: The marker written onto a step. Also how an already-marked step is
#: recognised, so a second pass (or a model that marked it itself) does not
#: stack prefixes.
MARKER = "Unverified — assumption"
_ALREADY_MARKED_RE = re.compile(r"^\s*(?:⚠️\s*)?unverified\b", re.IGNORECASE)

WARNING_SOURCE = "check_step_grounding"

#: Step index used for the preconditions in ``step_grounding`` and
#: ``unverified_steps``. Real steps are 1-based, as a tester reads them.
PRECONDITIONS = 0

# --- lexical backstop --------------------------------------------------------

#: camelCase or dotted camelCase: ``autoSync``, ``formsDocument.autoSync``.
#: Two leading lowercase letters keep "iOS"/"iPhone" out.
_IDENTIFIER_RE = re.compile(r"\b[a-z]{2,}[A-Z][A-Za-z0-9]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\b")
_NOT_IDENTIFIERS = frozenset({"macOS", "iPadOS", "watchOS", "tvOS", "visionOS"})
_FILE_SUFFIX_RE = re.compile(r"\.(?:tsx?|jsx?|py|swift|kt|cs|java|go|rb|json|ya?ml)$")

#: A quoted label: "Leave PDF", 'Save', “Connect”.
_QUOTED = r"[\"“'‘]([^\"”'’]{1,40})[\"”'’]"
#: One to three Capitalized words: Connect, Leave PDF, Save Net Sheet.
_CAPITALIZED = r"([A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){0,2})"

_UI_VERBS = (
    r"(?i:using|via|tap|taps|tapping|click|clicks|clicking|press|presses|pressing|"
    r"select|selects|selecting|choose|chooses|choosing|toggle|toggles|toggling)"
)
_VERB_THEN_NAME_RE = re.compile(
    rf"\b{_UI_VERBS}\s+(?i:the\s+|a\s+|an\s+)?(?:{_QUOTED}|{_CAPITALIZED}\b)"
)
_CONTROL_NOUNS = (
    r"(?:button|link|tab|menu|option|toggle|switch|checkbox|dialog|modal|sheet|"
    r"icon|field|dropdown|flag|setting)"
)
_NAME_THEN_NOUN_RE = re.compile(rf"(?:{_QUOTED}|\b{_CAPITALIZED})\s+{_CONTROL_NOUNS}\b")

#: Capitalized words that open a sentence or name a place rather than a
#: control. A capitalized match made only of these is not a control name.
_NOT_CONTROL_WORDS = frozenset({
    "The", "A", "An", "This", "That", "It", "In", "On", "Then", "When", "If",
    "And", "Or", "Not", "No", "Each", "Every", "Any", "All",
})


def _clean(name: str) -> str:
    return " ".join(name.split()).strip(" .,:;")


def extract_named_references(text: str) -> list[dict]:
    """Controls and flags a piece of step text names literally.

    Deliberately narrow: it looks for *names*, not for actions. "Change a
    calculation input" names nothing and is never flagged here — a generic
    action is what the plan should say when it cannot ground a label, so
    flagging it would punish the rewrite this pass asks for. Actions without
    a name are the model's to declare in ``step_grounding``.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    found: list[dict] = []
    seen: set[str] = set()

    def add(element: str, kind: str) -> None:
        element = _clean(element)
        if not element or element.lower() in seen:
            return
        if all(w in _NOT_CONTROL_WORDS for w in element.split()):
            return
        seen.add(element.lower())
        found.append({"element": element, "kind": kind})

    for m in _IDENTIFIER_RE.finditer(text):
        # A file name is not a flag: `updateCalculationInputs.ts` is the
        # identifier `updateCalculationInputs` plus where it lives.
        name = _FILE_SUFFIX_RE.sub("", m.group(0))
        if name not in _NOT_IDENTIFIERS:
            add(name, "flag")
    for regex in (_VERB_THEN_NAME_RE, _NAME_THEN_NOUN_RE):
        for m in regex.finditer(text):
            name = m.group(1) or m.group(2)
            if name:
                add(name, "control")
    return found


# --- code sources ------------------------------------------------------------


def readable_code_paths(dev_infos: list[dict] | None) -> set[str]:
    """Paths of every file the linked PRs changed that GitHub actually returned.

    A PR whose enrichment failed contributes nothing: its file list is what a
    ``pr:`` citation would have to be checked against, and we never saw it.
    """
    paths: set[str] = set()
    for entry in dev_infos or []:
        if not isinstance(entry, dict):
            continue
        dev = entry.get("development_info") or {}
        if not isinstance(dev, dict):
            continue
        for pr in dev.get("pull_requests") or []:
            if not isinstance(pr, dict) or pr.get("github_enrichment_failed"):
                continue
            for fc in pr.get("files_changed") or []:
                name = fc.get("filename") if isinstance(fc, dict) else None
                if isinstance(name, str) and name.strip():
                    paths.add(name.strip())
    return paths


def _split_source(source: object) -> tuple[str, str]:
    if not isinstance(source, str) or ":" not in source:
        return "", ""
    kind, _, locator = source.partition(":")
    return kind.strip().lower(), locator.strip()


def _path_is_readable(locator: str, code_paths: set[str]) -> bool:
    """Whether a ``pr:`` locator names a changed file we read.

    Tolerates a cited path that is a suffix of the real one (the model often
    drops a monorepo prefix) or carries a ``:line``.
    """
    path = re.sub(r":\d+(?:-\d+)?$", "", locator).strip().lstrip("./")
    if not path:
        return False
    return any(p == path or p.endswith("/" + path) for p in code_paths)


def is_code_source(source: object, code_paths: set[str]) -> bool:
    kind, locator = _split_source(source)
    if kind == "testid":
        return bool(locator)
    if kind == "pr":
        return _path_is_readable(locator, code_paths)
    return False


def case_has_code_grounding(case: dict, code_paths: set[str]) -> bool:
    return any(is_code_source(s, code_paths) for s in case.get("grounded_in") or [])


# --- the check ---------------------------------------------------------------


def _declared(case: dict) -> list[dict]:
    out = []
    for entry in case.get("step_grounding") or []:
        if not isinstance(entry, dict):
            continue
        step = entry.get("step")
        element = entry.get("element")
        if not isinstance(step, int) or isinstance(step, bool):
            continue
        if not isinstance(element, str) or not element.strip():
            continue
        out.append({
            "step": step,
            "element": _clean(element),
            "kind": entry.get("kind") or "control",
            "source": entry.get("source") or "",
        })
    return out


def _texts(case: dict) -> list[tuple[int, str]]:
    out = []
    pre = case.get("preconditions")
    if isinstance(pre, str) and pre.strip():
        out.append((PRECONDITIONS, pre))
    for i, step in enumerate(case.get("steps") or [], start=1):
        if isinstance(step, str):
            out.append((i, step))
    return out


def find_unbacked_references(case: dict, code_paths: set[str]) -> list[dict]:
    """Every control/action/flag a case names without code behind it.

    Returns ``{step, element, kind, reason}`` dicts, step 0 meaning the
    preconditions. Declared references are judged by their own source;
    undeclared ones found in the text are judged by the case's
    ``grounded_in``, since that is all they have.
    """
    if not isinstance(case, dict):
        return []
    has_code = case_has_code_grounding(case, code_paths)
    texts = dict(_texts(case))
    unbacked: list[dict] = []
    covered: set[tuple[int, str]] = set()

    for ref in _declared(case):
        if ref["step"] not in texts:
            continue
        covered.add((ref["step"], ref["element"].lower()))
        if is_code_source(ref["source"], code_paths):
            continue
        kind, _ = _split_source(ref["source"])
        if kind == "pr":
            reason = (
                f"cites {ref['source']}, which is not among the PR files that "
                "could be read"
            )
        elif kind:
            reason = f"rests on {ref['source']} — ticket or spec text, not code"
        else:
            reason = "no code source given"
        unbacked.append({**{k: ref[k] for k in ("step", "element", "kind")}, "reason": reason})

    if has_code:
        return unbacked
    for step, text in texts.items():
        for ref in extract_named_references(text):
            if (step, ref["element"].lower()) in covered:
                continue
            covered.add((step, ref["element"].lower()))
            unbacked.append({
                "step": step,
                **ref,
                "reason": (
                    "the case cites no code (its grounded_in is ticket or spec "
                    "text only), so nothing shows this exists in the app"
                ),
            })
    return unbacked


def _mark(text: str, elements: list[str]) -> str:
    if _ALREADY_MARKED_RE.match(text):
        return text
    names = ", ".join(f"“{e}”" for e in elements)
    return f"{MARKER} ({names} not found in the linked code): {text}"


def _warning_ac_id(case: dict) -> str | None:
    for ac in case.get("covers_acs") or []:
        if isinstance(ac, str) and ac.strip():
            return ac.strip()
    return None


def check_step_grounding(test_plan, code_paths: set[str]) -> list[dict]:
    """Mark unbacked step references in place and return the warnings added.

    Mutates cases (step text, ``unverified_steps``,
    ``needs_manual_verification``) and appends to
    ``test_plan.grounding_warnings``. Never adds, removes or reorders a case
    or a step; moving a case is left to ``quarantine_ungrounded_cases``.
    """
    added: list[dict] = []
    warnings = list(getattr(test_plan, "grounding_warnings", None) or [])
    for section in CHECKED_SECTIONS:
        for case in getattr(test_plan, section, None) or []:
            unbacked = find_unbacked_references(case, code_paths)
            if not unbacked:
                continue
            by_step: dict[int, list[str]] = {}
            for ref in unbacked:
                by_step.setdefault(ref["step"], []).append(ref["element"])
            for step, elements in by_step.items():
                if step == PRECONDITIONS:
                    case["preconditions"] = _mark(case["preconditions"], elements)
                else:
                    case["steps"][step - 1] = _mark(case["steps"][step - 1], elements)
            case["unverified_steps"] = unbacked
            if any(ref["step"] != PRECONDITIONS for ref in unbacked):
                case["needs_manual_verification"] = True
            ac_id = _warning_ac_id(case)
            if ac_id is None:
                continue
            for ref in unbacked:
                where = "preconditions" if ref["step"] == PRECONDITIONS else f"step {ref['step']}"
                warning = {
                    "ac_id": ac_id,
                    "missing_element": ref["element"],
                    "explanation": (
                        f"Step check: “{case.get('title', '')}” {where} names "
                        f"{ref['kind']} “{ref['element']}”, but {ref['reason']}. "
                        "Confirm the app offers it in that state before running "
                        "the step, or perform the intent another way."
                    ),
                    "source": WARNING_SOURCE,
                    "severity": "warn",
                }
                warnings.append(warning)
                added.append(warning)
    if added:
        test_plan.grounding_warnings = warnings
    return added
