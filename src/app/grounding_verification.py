"""Re-read every code claim a plan makes, before it is saved, and downgrade the ones that fail.

The generator is told to quote labels from the code, restate rules whole, and
say which token a backend case needs (``source_facts`` shows it all three).
This pass is the other half: it does not trust that it did. For every case in
the gradeable sections it opens the cited file at the PR's head commit and
checks four kinds of claim:

``label``
    Text a step tells the tester to find or tap — a quoted label in a UI
    context, or a ``step_grounding`` entry's ``ui_text`` — must be a string
    literal in the cited source (its own literals, or the localization value of
    a key it references). Fails as SK-2719's 'Sign In Person' did, where the code
    says "Sign In-Person". The step is marked "Unverified — label per ticket, not
    found in code" with what the code has instead, when a near match exists.

``element``
    A ``step_grounding`` entry that names a flag or identifier must find it in
    the file it cites; one that gives a line range must find its text there.

``expected``
    A verified expected result (``expected_verified: true``) must cite lines or
    a function that exist, that say something the expected result says, and
    that the generator was shown. When the case restates a rule from that code
    — two or more of its conditions are named — every condition of the whole
    function, its later filter/min steps and its same-file helpers must be
    named, and no quoted value the code lacks may be added. SK-2719's
    integration_tests:1 named four of seven. A failure sets
    ``expected_verified: false`` and records why.

``client_token``
    A backend case that reproduces a call a client app makes must state the
    scopes that app's token carries, citing its auth config. Where it does not,
    the facts are attached from the config (they are code, not a guess). When
    no configured runner token source carries those scopes, the case is marked
    ``needs_client_equivalent_token`` so a runner reports it blocked.

What it never does
------------------
* Remove, add or reorder a case or a step. Text is prefixed in place, like
  ``step_grounding``'s markers, so case ids are unchanged. Setting
  ``expected_verified: false`` can let quarantine move a case that is *also*
  ``needs_manual_verification`` — the existing definition of a case nothing
  about which traces to code — and only then.
* Call a failed read a failure. A file GitHub would not return makes its claims
  ``unchecked``, counted separately; "could not re-read" is not "not in the
  code" (the repo's recurring bug class, see MEMORY: transient failure stated
  as fact).

The result goes into ``source_provenance["grounding_verification"]`` with the
count of downgraded claims, and onto each case as ``grounding_downgrades``.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from . import source_facts as sf
from .source_facts import SourceFile, SourceIndex

#: Same sections the step check and quarantine work on. Security cases are
#: request-level by construction and carry their own persona/auth fields.
CHECKED_SECTIONS = ("happy_path", "edge_cases", "integration_tests")

LABEL_MARKER = "Unverified — label per ticket, not found in code"
ELEMENT_MARKER = "Unverified — not found in the cited code"
_ALREADY_MARKED_RE = re.compile(r"^\s*(?:⚠️\s*)?unverified\b", re.IGNORECASE)

NEEDS_TOKEN = "needs client-equivalent token"


@dataclass
class Outcome:
    case_id: str
    kind: str          # label | element | expected | client_token
    claim: str
    status: str        # verified | downgraded | unchecked
    reason: str = ""
    source: str = ""

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v != ""}


# --- what the generator was shown -------------------------------------------


def shown_lines(dev_infos: list[dict] | None, *, multi: bool = False, facts: dict | None = None,
                repo_evidence: list[dict] | None = None, exploration: dict | None = None,
                full_file_budget: int | None = None) -> dict[str, set[int]]:
    """path → line numbers whose text reached the generator's prompt.

    From the same budget functions the prompt builders call
    (``diff_budget``), the whole functions in the source-facts section, test
    files shown in full (repo evidence), and the line ranges exploration
    findings cite — the generator was told to cite those as given.
    """
    from .diff_budget import (PATCH_BUDGET_MULTI, PATCH_BUDGET_SINGLE, shown_full_file_lines,
                              shown_patch_lines)

    total, cap = PATCH_BUDGET_MULTI if multi else PATCH_BUDGET_SINGLE
    out: dict[str, set[int]] = {}

    def add(path: str, lines) -> None:
        out.setdefault(path, set()).update(lines)

    prs = [pr for d in dev_infos or [] for pr in ((d or {}).get("pull_requests") or []) if isinstance(pr, dict)]
    full_prs = sum(1 for pr in prs if any(f.get("full_content") for f in pr.get("files_changed") or []))
    for pr in prs:
        files = [f for f in pr.get("files_changed") or [] if isinstance(f, dict)]
        for path, lines in shown_patch_lines(files, total_budget=total, per_file_cap=cap).items():
            add(path, lines)
        if not multi and full_prs and full_file_budget:
            for path, lines in shown_full_file_lines(files, total_budget=full_file_budget // full_prs).items():
                add(path, lines)
    for path, ranges in ((facts or {}).get("shown") or {}).items():
        for a, b in ranges:
            add(path, range(a, b + 1))
    for e in repo_evidence or []:
        for path, text in (e.get("test_sources") or {}).items():
            add(path, range(1, text.count("\n") + 2))
    for f in (exploration or {}).get("findings") or []:
        m = re.match(r"(\d+)(?:\s*-\s*(\d+))?", str(f.get("lines") or ""))
        if m and f.get("path"):
            add(f["path"], range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1))
    return out


def _shown_for(shown: dict[str, set[int]], path: str) -> set[int]:
    for p, lines in shown.items():
        if p == path or p.endswith("/" + path) or path.endswith("/" + p):
            return lines
    return set()


def _ranges(lines: list[int]) -> str:
    lines = sorted(set(lines))
    out, start = [], None
    for i, n in enumerate(lines):
        if start is None:
            start = n
        if i + 1 == len(lines) or lines[i + 1] != n + 1:
            out.append(f"{start}" if start == n else f"{start}-{n}")
            start = None
    return ", ".join(out[:6]) + (" …" if len(out) > 6 else "")


# --- helpers -----------------------------------------------------------------


def _kind(source: object) -> tuple[str, str]:
    if not isinstance(source, str) or ":" not in source:
        return "", ""
    kind, _, loc = source.partition(":")
    return kind.strip().lower(), loc.strip()


def _code_sources(index: SourceIndex, sources: list[str]) -> tuple[list[SourceFile], list[str]]:
    """Readable files among ``pr:`` sources, and reasons for the unreadable."""
    files, unreadable = [], []
    for s in sources:
        kind, loc = _kind(s)
        if kind != "pr":
            continue
        got = index.resolve(loc)
        if isinstance(got, SourceFile):
            if got not in files:
                files.append(got)
        elif isinstance(got, str):
            unreadable.append(got)
    return files, unreadable


def _prose(case: dict) -> str:
    steps = [s for s in case.get("steps") or [] if isinstance(s, str)]
    return "\n".join(steps + [case.get("expected") or ""])


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;!?])\s+|\n+", text or "") if s.strip()]


def _mark(text: str, marker: str, detail: str) -> str:
    if _ALREADY_MARKED_RE.match(text or ""):
        return text
    return f"{marker} ({detail}): {text}"


def _label_detail(label: str, near: list[sf.UIString], cited: list[SourceFile]) -> str:
    if near:
        alt = near[0]
        return f"“{label}”; the cited code has “{alt.text}” at {alt.where()}"
    names = ", ".join(posixpath.basename(f.path) for f in cited[:3])
    return f"“{label}” is not a string in {names}"


def _downgrade(case: dict, outcome: Outcome) -> None:
    case.setdefault("grounding_downgrades", []).append(
        {k: v for k, v in outcome.as_dict().items() if k != "case_id"})


# --- label and element claims -----------------------------------------------


def _verify_labels(case_id: str, case: dict, index: SourceIndex) -> list[Outcome]:
    out: list[Outcome] = []
    entries = [e for e in case.get("step_grounding") or [] if isinstance(e, dict)]
    case_sources = [s for s in case.get("grounded_in") or [] if isinstance(s, str)]
    steps = case.get("steps") or []
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, str) or _ALREADY_MARKED_RE.match(step):
            continue
        mine = [e for e in entries if e.get("step") == i]
        labels = sf.quoted_labels(step)
        # An entry with no code source has already said the element is not in
        # the code; check_step_grounding marks that step, this pass does not.
        for e in mine:
            if _kind(e.get("source"))[0] != "pr":
                continue
            for t in [e.get("ui_text")] + sf.QUOTED_RE.findall(e.get("element") or ""):
                if isinstance(t, str) and t.strip() and t.strip() not in labels:
                    labels.append(t.strip())
        if not labels:
            continue
        # The step's own sources first, then everything else the case cites:
        # a step that taps a menu row often has its grounding entry on the
        # step that opens the menu.
        cited = [e.get("source") for e in mine if _kind(e.get("source"))[0] == "pr"]
        files, unreadable = _code_sources(index, cited + [s for s in case_sources if s not in cited])
        if not files:
            for label in labels:
                if unreadable:
                    out.append(Outcome(case_id, "label", f"step {i}: “{label}”", "unchecked",
                                       "; ".join(unreadable[:2])))
            continue
        literals = [lit for f in files for lit in index.literals_for(f)]
        failed: list[str] = []
        for label in labels:
            hit = sf.find_literal(label, literals) or _unique_elsewhere(label, index, files)
            if hit:
                out.append(Outcome(case_id, "label", f"step {i}: “{label}”", "verified",
                                   source=f"{hit.path}:{hit.line}"))
                continue
            near = sf.near_literals(label, literals) or sf.near_literals(label, _all_literals(index))
            detail = _label_detail(label, near, files)
            failed.append(detail)
            o = Outcome(case_id, "label", f"step {i}: “{label}”", "downgraded", detail,
                        ", ".join(posixpath.basename(f.path) for f in files[:3]))
            out.append(o)
            _downgrade(case, o)
            case.setdefault("unverified_labels", []).append(
                {"step": i, "label": label, "code_has": near[0].text if near else None,
                 "code_at": f"{near[0].path}:{near[0].line}" if near else None})
        if failed:
            steps[i - 1] = _mark(step, LABEL_MARKER, "; ".join(failed))
    return out


def _all_literals(index: SourceIndex) -> list[sf.UIString]:
    return [lit for f in index.files.values() if index.changed_lines.get((f.repo, f.path))
            for lit in sf.ui_strings(f.path, f.text)]


def _unique_elsewhere(label: str, index: SourceIndex, cited: list[SourceFile]) -> sf.UIString | None:
    """A UI string the label quotes exactly, defined in one changed file only.

    The case did not cite that file, but the text is the code's, not the
    ticket's, and there is exactly one place it can come from. A label found
    in several files ("Save") stays unverified: which one the step means is
    the question the citation was meant to answer.
    """
    hits = [lit for lit in _all_literals(index) if sf._canon(lit.text) == sf._canon(label)]
    paths = {h.path for h in hits}
    return hits[0] if len(paths) == 1 and not any(f.path in paths for f in cited) else None


_CODE_ID_RE = re.compile(r"\b(?:[a-z]+(?:_[a-z0-9]+)+|[a-z]{2,}[A-Z]\w*(?:\.\w+)*)\b")


def _verify_elements(case_id: str, case: dict, index: SourceIndex) -> list[Outcome]:
    """Flags and identifiers a step_grounding entry names, in the file it cites."""
    out: list[Outcome] = []
    for e in case.get("step_grounding") or []:
        if not isinstance(e, dict):
            continue
        kind, loc = _kind(e.get("source"))
        if kind != "pr":
            continue
        got = index.resolve(loc)
        ids = [m for m in _CODE_ID_RE.findall(e.get("element") or "")
               if not re.search(r"\.(?:swift|kt|tsx?|jsx?|py|cs|java)$", m)]
        if not ids:
            continue
        claim = f"step {e.get('step')}: {', '.join(ids)}"
        if isinstance(got, str):
            out.append(Outcome(case_id, "element", claim, "unchecked", got))
            continue
        if got is None:
            continue  # not a readable PR file: check_step_grounding already marked it
        _, rng, _ = sf.parse_locator(loc)
        text = "\n".join(got.lines[rng[0] - 1: rng[1]]) if rng and rng[1] <= len(got.lines) else got.text
        if isinstance(e.get("lines"), str):
            m = re.match(r"(\d+)(?:\s*-\s*(\d+))?", e["lines"])
            if m:
                text = "\n".join(got.lines[int(m.group(1)) - 1: int(m.group(2) or m.group(1))])
        missing = [i for i in ids if i not in text]
        if not missing:
            out.append(Outcome(case_id, "element", claim, "verified", source=got.path))
            continue
        elsewhere = sorted({posixpath.basename(f.path) for f in index.files.values()
                            for i in missing if f is not got and i in f.text})
        o = Outcome(case_id, "element", claim, "downgraded",
                    f"{', '.join(missing)} not in {posixpath.basename(got.path)}"
                    + (f" lines {rng[0]}-{rng[1]}" if rng else "")
                    + (f" (found in {', '.join(elsewhere[:3])})" if elsewhere else ""), got.path)
        out.append(o)
        _downgrade(case, o)
        e["verified"] = False
        step = e.get("step")
        detail = f"“{', '.join(missing)}” not in {posixpath.basename(got.path)}"
        if step == 0 and isinstance(case.get("preconditions"), str):
            case["preconditions"] = _mark(case["preconditions"], ELEMENT_MARKER, detail)
        elif isinstance(step, int) and 0 < step <= len(case.get("steps") or []):
            case["steps"][step - 1] = _mark(case["steps"][step - 1], ELEMENT_MARKER, detail)
    return out


# --- rule restatements and expected results ---------------------------------


@dataclass
class Region:
    src: SourceFile
    functions: list[sf.Function]
    cited_range: tuple[int, int] | None

    def lines(self) -> list[int]:
        out = {n for f in self.functions for n in range(f.start, f.end + 1)}
        if self.cited_range and not self.functions:
            out |= set(range(self.cited_range[0], self.cited_range[1] + 1))
        return sorted(out)

    def text(self) -> str:
        return "\n".join(self.src.lines[n - 1] for n in self.lines() if 0 < n <= len(self.src.lines))

    def label(self) -> str:
        names = [f.name for f in self.functions]
        where = f"{posixpath.basename(self.src.path)}:{_ranges(self.lines())}"
        return f"{where} ({', '.join(names)})" if names else where


def _region(src: SourceFile, rng: tuple[int, int] | None, symbols: list[str], prose: str) -> Region:
    fns = sf.functions(src.path, src.text)
    targets: list[sf.Function] = []
    for name in symbols + [m for m in re.findall(r"\b[a-z]\w*[A-Z]\w*\b", prose)]:
        fn = sf.function_named(fns, name)
        if fn and fn not in targets:
            targets.append(fn)
    if rng:
        enclosing = sf.enclosing_function(fns, rng[0])
        # The whole function: a range inside it can stop before the later
        # filter/min steps in the same return path, which is SK-2719's miss.
        if enclosing and enclosing not in targets:
            targets.append(enclosing)
    group: list[sf.Function] = []
    for t in targets:
        for f in sf.with_helpers(t, fns):
            if f not in group:
                group.append(f)
    return Region(src, group, rng)


def _overlap(region_text: str, prose: str) -> bool:
    """Whether the cited lines share any identifier, value or number with the claim."""
    hay = prose.lower()
    tokens = set()
    for v in re.findall(r'"([^"\n]{2,60})"|\'([^\'\n]{2,60})\'', region_text):
        tokens.add((v[0] or v[1]).lower())
    for ident in re.findall(r"[A-Za-z_]\w{3,}", region_text):
        tokens.update(w for w in sf._words(ident) if len(w) >= 4)
        tokens.add(ident.lower())
    tokens.update(re.findall(r"\b\d{3}\b", region_text))
    for t in tokens:
        if t in hay or (len(t) >= 5 and re.search(rf"\b{re.escape(t[:5])}", hay)):
            return True
    return False


def check_restatement(region: Region, prose: str, known_text: set[str] | None = None) -> dict | None:
    """Missing conditions and added values when ``prose`` restates the region's rule.

    None when the prose does not restate it (it names fewer than two of the
    rule's conditions): an expected result that cites a handler for its status
    code is not a summary of the handler's logic.
    """
    atoms = sf.condition_atoms(region.src.path, [
        (n, region.src.lines[n - 1]) for f in region.functions for n in range(f.start + 1, f.end + 1)])
    if len(atoms) < 2:
        return None
    mentioned = sf.atoms_mentioned(atoms, prose)
    if len(mentioned) < 2:
        return None
    missing = [a for a in atoms if a not in mentioned]
    restating = [s for s in _sentences(prose) if len(sf.atoms_mentioned(atoms, s)) >= 2]
    code = sf._canon(region.text()).lower()
    # Added values, not added words: a quoted value the rule is said to test
    # ('Approver' beside 'Signer') that the code never mentions. Identifiers
    # are not checked — a restating sentence routinely names the file or the
    # endpoint the data comes from, and that is not a condition of the rule.
    # Nor is a quoted on-screen label (``known_text``, the changed code's UI
    # strings): "tap 'Sign In-Person'" in the same sentence adds no condition.
    extras: list[str] = []
    for s in restating:
        for v in sf.QUOTED_RE.findall(s):
            if (v.strip() and sf._canon(v).lower() not in code and f"“{v}”" not in extras
                    and not (known_text and sf._canon(v) in known_text)):
                extras.append(f"“{v}”")
    return {"atoms": atoms, "missing": missing, "extras": extras}


def _verify_expected(case_id: str, case: dict, index: SourceIndex,
                     shown: dict[str, set[int]] | None) -> list[Outcome]:
    if case.get("expected_verified") is not True:
        return []
    source = case.get("expected_source") or ""
    claim = f"expected verified against {source or '(no source)'}"
    path, rng, symbols = sf.parse_locator(source)
    got = index.resolve(path) if path else None
    if isinstance(got, str):
        return [Outcome(case_id, "expected", claim, "unchecked", got)]
    reasons: list[str] = []
    prose = _prose(case)
    region = None
    if got is None:
        reasons.append(f"cites {path or 'no file'}, which is not a file in the linked PRs' repos"
                       if path else "claims a verified expected result but cites no source")
    else:
        if rng and rng[1] > len(got.lines):
            reasons.append(f"cites lines {rng[0]}-{rng[1]} of {posixpath.basename(got.path)}, "
                           f"which has {len(got.lines)}")
        else:
            region = _region(got, rng, symbols, prose)
            if not region.lines():
                reasons.append(f"names {posixpath.basename(got.path)} with no line range or function, "
                               "so there are no cited lines to re-read")
                region = None
    others, _ = _code_sources(index, case.get("grounded_in") or [])
    known_text = {sf._canon(lit.text) for lit in _all_literals(index)}
    # A declared logic_claims rule is the restatement: checked whole, and shown
    # to the tester beside the expected result. When it names every condition,
    # the prose may refer to the rule ("because the picker keeps only the
    # lowest group") without repeating all of it.
    declared: set[str] = set()
    for lc in case.get("logic_claims") or []:
        names, problems = _verify_logic_claim(lc, index, known_text)
        reasons += problems
        if not problems:
            declared |= names
    if region is not None:
        covered = bool(region.functions) and {f.name for f in region.functions} <= declared
        restated = (check_restatement(region, prose, known_text)
                    if region.functions and not covered else None)
        if restated:
            if restated["missing"]:
                reasons.append(
                    "restates the rule in " + region.label() + " without "
                    + "; ".join(f"{a.display} (line {a.line})" for a in restated["missing"]))
            if restated["extras"]:
                reasons.append("adds " + ", ".join(restated["extras"]) + ", which the cited code does not contain")
        elif not covered:
            cited_text = (
                "\n".join(got.lines[max(0, rng[0] - 3): rng[1] + 2]) if rng else region.text())
            if not _overlap(cited_text, case.get("expected") or ""):
                reasons.append(f"the cited lines ({region.label()}) contain nothing the expected result states")
        if shown is not None:
            seen = _shown_for(shown, got.path)
            whole = restated or covered
            need = region.lines() if whole else (list(range(rng[0], rng[1] + 1)) if rng else region.lines())
            unseen = [n for n in need if n not in seen]
            if unseen and (whole or len(unseen) == len(need)):
                reasons.append(
                    f"the generator was not shown {posixpath.basename(got.path)} lines {_ranges(unseen)}"
                    + (" — the rule spans code it did not read" if whole else ""))
    # Labels an expected result quotes are claims about the code too.
    files = ([got] if isinstance(got, SourceFile) else []) + [f for f in others if f is not got]
    if files:
        literals = [lit for f in files for lit in index.literals_for(f)]
        for label in sf.quoted_labels(case.get("expected") or ""):
            if not (sf.find_literal(label, literals) or _unique_elsewhere(label, index, files)):
                reasons.append("quotes " + _label_detail(label, sf.near_literals(label, literals), files))
    if not reasons:
        return [Outcome(case_id, "expected", claim, "verified", source=source)]
    o = Outcome(case_id, "expected", claim, "downgraded", "; ".join(reasons), source)
    case["expected_verified"] = False
    case["expected_verification_downgraded"] = o.reason
    _downgrade(case, o)
    return [o]


def _verify_logic_claim(lc: dict, index: SourceIndex, known_text: set[str]) -> tuple[set[str], list[str]]:
    """A declared ``logic_claims`` entry must name every condition of the code
    it cites, and add no value that code lacks. Returns the functions it
    covers and what is wrong with it.

    Its wording need not appear in the steps — the model writes the rule out
    whole here and refers to it in prose — which is why it is rendered to the
    tester beside the expected result.
    """
    if not isinstance(lc, dict):
        return set(), []
    rule = (lc.get("rule") or "").strip()
    path, rng, symbols = sf.parse_locator(lc.get("source") or "")
    if lc.get("function"):
        symbols = [lc["function"]] + symbols
    got = index.resolve(path) if path else None
    if not isinstance(got, SourceFile) or not rule:
        return set(), []
    region = _region(got, rng, symbols, "")
    if not region.functions:
        return set(), []
    restated = check_restatement(region, rule, known_text)
    if restated is None:
        atoms = sf.condition_atoms(region.src.path, [
            (n, region.src.lines[n - 1]) for f in region.functions for n in range(f.start + 1, f.end + 1)])
        # A function with fewer than two conditions cannot be named "too
        # little": there is no rule to summarise partially. Naming its one
        # condition (or citing a condition-free helper) covers it.
        if len(atoms) < 2 and len(sf.atoms_mentioned(atoms, rule)) == len(atoms):
            return {f.name for f in region.functions}, []
        return set(), [f"its logic_claims rule names too little of {region.label()} to check"]
    problems = []
    if restated["missing"]:
        problems.append("the claimed rule omits " + "; ".join(
            f"{a.display} (line {a.line})" for a in restated["missing"]))
    if restated["extras"]:
        problems.append("the claimed rule adds " + ", ".join(restated["extras"]))
    return {f.name for f in region.functions}, problems


# --- client tokens -----------------------------------------------------------

_PLATFORM_WORDS = {
    "ios": r"\biOS\b|\biPhone\b|\biPad\b|\bmobile app\b",
    "android": r"\bAndroid\b|\bmobile app\b",
    "web": r"\bweb app\b|\bbrowser\b|\bfront[- ]?end\b|\bweb client\b",
}


def _pick_config(configs: list[dict], case: dict) -> dict | None:
    env_text = " ".join([case.get("environment") or "", case.get("preconditions") or ""]).lower()
    for c in configs:
        if c.get("env") and re.search(rf"(?<![a-z]){re.escape(c['env'])}", env_text):
            return c
    non_prod = [c for c in configs if c.get("env") not in ("prod",)]
    return (non_prod or configs or [None])[0]


def _mirrors(case: dict, index: SourceIndex, repo: str, platform: str) -> bool:
    if isinstance(case.get("client_token"), dict):
        return True
    cited = [case.get("expected_source") or ""] + [s for s in case.get("grounded_in") or [] if isinstance(s, str)]
    cited += [e.get("source") or "" for e in case.get("step_grounding") or [] if isinstance(e, dict)]
    for c in cited:
        for r, _ in index.candidates(sf.parse_locator(c)[0]):
            if r == repo:
                return True
    text = " ".join([_prose(case), case.get("title") or "", case.get("credentials") or ""])
    return bool(re.search(_PLATFORM_WORDS.get(platform, r"$^"), text, re.I))


def _verify_client_token(case_id: str, case: dict, index: SourceIndex,
                         token_sources: list[dict] | None) -> list[Outcome]:
    if case.get("surface") != "backend_http" or not index.auth_configs:
        return []
    by_repo: dict[str, list[dict]] = {}
    for c in index.auth_configs:
        by_repo.setdefault(c["repo"], []).append(c)
    out: list[Outcome] = []
    for repo, configs in by_repo.items():
        platform = configs[0].get("platform") or ""
        if not _mirrors(case, index, repo, platform):
            continue
        cfg = _pick_config(configs, case)
        if not cfg:
            continue
        required = sf.api_scopes(cfg["scopes"])
        cite = f"{cfg['path']}:{cfg['line']}"
        stated_text = " ".join([case.get("credentials") or "", str(case.get("client_token") or "")])
        claim = f"token for a call the {platform} app makes"
        declared = (case.get("client_token") or {}).get("scopes") if isinstance(case.get("client_token"), dict) else None
        extra = [s for s in (declared or []) if s not in cfg["scopes"]]
        stated = all(s in stated_text for s in required) and posixpath.basename(cfg["path"]) in stated_text
        if stated and not extra:
            out.append(Outcome(case_id, "client_token", claim, "verified", source=cite))
        else:
            why = (f"names scopes {', '.join(extra)} that {posixpath.basename(cfg['path'])} does not request"
                   if extra else f"does not state the scopes the {platform} app's token carries ({cite})")
            o = Outcome(case_id, "client_token", claim, "downgraded", why, cite)
            out.append(o)
            _downgrade(case, o)
        sources = [s for s in token_sources or [] if isinstance(s, dict)]
        able = [s for s in sources
                if set(required) <= set(s.get("scopes") or [])
                and (not cfg.get("audience") or s.get("audience") in (None, cfg["audience"]))]
        token = {
            "client": repo, "platform": platform, "scopes": cfg["scopes"], "required_scopes": required,
            "audience": cfg.get("audience"), "source": cite, "requires_client_scopes": bool(required),
            "runner_source": able[0].get("name") if able else None,
        }
        case["client_token"] = token
        if required and not able:
            case["needs_client_equivalent_token"] = True
            case["needs_client_equivalent_token_reason"] = (
                f"the {platform} app's token carries {' '.join(required)} ({cite}); "
                + ("no configured runner token source carries all of them"
                   if sources else "no runner token sources are configured (RUNNER_TOKEN_SOURCES)")
                + " — report this case as BLOCKED, not failed")
        break
    return out


# --- the pass ----------------------------------------------------------------


def verify_plan(test_plan, index: SourceIndex, *, shown: dict[str, set[int]] | None = None,
                token_sources: list[dict] | None = None) -> dict:
    """Check every case's code claims in place and return the report.

    The report is what ``source_provenance["grounding_verification"]`` stores:
    counts by status and kind, the downgraded and unchecked claims, and the
    ids of cases that need a client-equivalent token. Case ids are body
    positions before quarantine, like the reachability record.
    """
    outcomes: list[Outcome] = []
    blocked: list[str] = []
    for section in CHECKED_SECTIONS:
        for i, case in enumerate(getattr(test_plan, section, None) or []):
            if not isinstance(case, dict):
                continue
            cid = f"{section}:{i}"
            outcomes += _verify_labels(cid, case, index)
            outcomes += _verify_elements(cid, case, index)
            outcomes += _verify_expected(cid, case, index, shown)
            outcomes += _verify_client_token(cid, case, index, token_sources)
            if case.get("needs_client_equivalent_token"):
                blocked.append(cid)
    by_kind: dict[str, dict[str, int]] = {}
    for o in outcomes:
        by_kind.setdefault(o.kind, {"verified": 0, "downgraded": 0, "unchecked": 0})[o.status] += 1
    return {
        "checked": sum(1 for o in outcomes if o.status != "unchecked"),
        "downgraded": sum(1 for o in outcomes if o.status == "downgraded"),
        "unchecked": sum(1 for o in outcomes if o.status == "unchecked"),
        "by_kind": by_kind,
        "downgraded_claims": [o.as_dict() for o in outcomes if o.status == "downgraded"][:60],
        "unchecked_claims": [o.as_dict() for o in outcomes if o.status == "unchecked"][:30],
        "needs_client_equivalent_token": blocked,
        "ids_are": "raw body positions before quarantine",
    }


async def run_grounding_verification(test_plan, index: SourceIndex | None, github, *,
                                     shown: dict[str, set[int]] | None,
                                     token_sources: list[dict] | None) -> dict:
    """Read any cited file the index lacks, then ``verify_plan``. Never raises:
    a pass that cannot run says so in the report instead of reporting a clean
    plan."""
    if index is None:
        return {"error": "source files were not read, so no claim was re-checked",
                "checked": 0, "downgraded": 0, "unchecked": 0}
    cited: list[str] = []
    for section in CHECKED_SECTIONS:
        for case in getattr(test_plan, section, None) or []:
            if isinstance(case, dict) and case.get("expected_verified") is True and case.get("expected_source"):
                cited.append(sf.parse_locator(case["expected_source"])[0])
    try:
        if github is not None:
            await sf.read_cited(index, github, cited)
        return verify_plan(test_plan, index, shown=shown, token_sources=token_sources)
    except Exception as e:  # noqa: BLE001 — a broken check must not pass as a clean plan
        return {"error": f"grounding verification failed: {type(e).__name__}: {e}",
                "checked": 0, "downgraded": 0, "unchecked": 0}
