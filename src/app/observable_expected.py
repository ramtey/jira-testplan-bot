"""Rule AK: an Expected asserts what a tester can observe, never which internal path ran.

SK-3014 plan 734 v1, edge_cases:4: "The RPC created the row first, and
upsertStreamedText then took the 23505 → update path with no error." A
black-box tester cannot see which branch ran, and the claim was false for the
case's own steps: ``upsertStreamedText`` checks for the row first
(streamedTextsRepository.ts:114) and, when the refusal RPC has already created
it, goes straight to the owner-scoped update (:139-150). The 23505 fallback
(:128-135) runs only when another write creates the row between that check and
the insert — a race a single sequential first message never produces.

Three parts, all marking in place:

``check_observable_expected`` (code; before the trace critic and again before save)
    Splits every Expected (and ``expected_outcome``) into clauses. A clause that
    names an error code, an internal branch / fallback / race, or a function
    call, and has no observable check (a response, a stored row, a log line, on-
    screen text — in the clause, or the same code named in ``how_to_verify``),
    leaves the Expected for ``mechanism_note``, labelled as the reason and never
    a pass condition. An Expected left with nothing observable is marked.

The trace critic's kind 4 (``internal_path`` items, plan_recheck)
    Traces whether the case's steps reach the path a note names. When they do
    not, or reach it only under a race or a fault, the note is replaced with
    the path they do take and says so.

``label_unchecked_race_paths`` (code; before save)
    A note naming a race-only fallback (unique violation, serialization
    failure, deadlock, version conflict) that the critic did not confirm, on a
    case whose steps never run anything concurrently, is labelled unverified.
"""

from __future__ import annotations

import re
from typing import Any

GRADED = ("happy_path", "edge_cases", "integration_tests", "security_negative_tests")

NOTE_FIELD = "mechanism_note"
NOTE_LABEL = "Why (the reason, not a pass condition — rule AK):"
UNOBSERVABLE_MARKER = ("Not observable as written (rule AK): this Expected only names internal code paths — "
                       "record the response, the stored data and any log line instead:")
RACE_UNCHECKED = ("Unverified (rule AK): {what} normally runs only when another write lands between the "
                  "existence check and the insert; these sequential steps may not reach it.")
NOT_REACHED_LABEL = "Not reached by these steps (rule AK):"

# --- what names an internal mechanism ---------------------------------------------------------

_IDENT = r"`?(?:[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*|[a-z0-9]+(?:_[a-z0-9]+){1,}|[A-Z][a-z0-9]+[A-Z]\w*\.[a-z]\w*)`?"
_CODEISH = r"(?:path|branch|handler|catch|update|insert|upsert|query|call|retry|fallback)"

# Database / driver error codes. A bare 5-digit number is a code only next to a word that says so
# ("the 23505 → update path", "Postgres error 23505"): amounts and window sizes (51200, 60000) are not.
_ERROR_CODE_RE = re.compile(
    r"\b(?:SQLSTATE|Postgres(?:QL)?\s+error|error\s+code|code)\s*[:=]?\s*`?[0-9A-Z]{5}\b"
    r"|(?<![\w./:-])`?(?:\d{5}|\d{2}P\d{2})`?(?:\s*(?:->|→)\s*\w+)?\s+(?:path|branch|fallback)\b"
    r"|(?<![\w./:-])`?(?:\d{5}|\d{2}P\d{2})`?\s+(?:error|violation|exception)\b"
    rf"|(?<![\w./:-])(?:P2002|E11000)\b"
    r"|\b(?:UNIQUE_VIOLATION|FOREIGN_KEY_VIOLATION|SERIALIZATION_FAILURE|DEADLOCK_DETECTED|ER_DUP_ENTRY|"
    r"SQLITE_CONSTRAINT\w*|[A-Z][A-Z0-9]*_(?:VIOLATION|EXCEPTION))\b",
)
# Which branch ran: a path, branch, fallback, catch, retry, race. Arrows alone are not branches
# ("3741.88 → summerTaxGlobal" is an input-to-output table).
_BRANCH_RE = re.compile(
    r"\b(?:took|takes|taking|take|goes|went|go|routes?)\s+(?:the\s+|its\s+|a\s+|down\s+the\s+|through\s+the\s+|"
    r"into\s+the\s+)?[\w`'\"→>.\- ]{0,40}?\b(?:path|branch)\b"
    rf"|\b(?:code\s+path|branch\s+(?:runs|ran|is\s+taken|was\s+taken)|"
    rf"(?:falls?|falling|fell)\s+back\s+to\s+(?:the\s+|an?\s+)?(?:(?-i:{_IDENT})|\w+\s+{_CODEISH}\b|{_CODEISH}\b)|"
    r"fallback\s+(?:runs|ran|is\s+taken|kicks\s+in)|catch\s+block|catch(?:es)?\s+the\s+\w+\s+(?:error|exception)|"
    r"re-?throws?|swallow(?:s|ed)|short-circuits?|"
    r"retr(?:y|ies|ied)\s+(?:the\s+)?(?:insert|update|write|query|upsert)|"
    r"race(?:\s+condition)?\s+(?:is|was|gets)\s+handled|(?:insert|write)\s+race)\b",
    re.I,
)
# A function call: foo(), Class.method(), or a code identifier that "calls"/"runs"/"is called".
_FUNC_CALL_RE = re.compile(r"(?<![\w.])[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\(\s*\)")
_CALLED_RE = re.compile(
    rf"\b(?:calls?|called|invokes?|invoked|runs?|ran|executes?|executed)\s+(?:the\s+)?{_IDENT}"
    rf"|{_IDENT}\s+(?:is|was|gets|got)\s+(?:called|invoked|run|executed)"
    rf"|{_IDENT}\s+(?:then\s+|first\s+|now\s+)?(?:took|takes|runs|ran|calls|called|catches|caught|retries|retried)\b",
)

# --- what a tester can observe ------------------------------------------------------------------

_OBSERVABLE_RE = re.compile(
    r"(?:^\s*`?[1-5]\d\d\b|\b[1-5]\d\d\s*[{'\"(`]|\b(?:fails?|failed|rejected|errors?)\s+with\s+['\"“`]|"
    r"\blogs?\s+(?:only\s+)?['\"`{“]|\b(?:message|error)\s+['\"“]|\(\s*[1-5]\d\d\b|\b[1-5]\d\d(?:\s*/\s*[1-5]\d\d)+)"
    r"|\b(?:responses?|responds|status(?:\s+code)?|HTTP\s*\d{3}|returns?\s+(?:a\s+|an\s+|HTTP\s+)?[1-5]\d\d\b|"
    r"[1-5]\d\d\s+(?:OK|Created|No Content|Bad Request|Unauthorized|Forbidden|Not Found|Conflict)|"
    r"body|payload|JSON|header|"
    r"shows?|shown|displays?|displayed|appears?|visible|re-?render(?:s|ed)?|renders?|rendered|API\s+errors?|"
    r"error\s+responses?|on\s+(?:the\s+)?screen|page|toast|alert|banner|dialog|"
    r"modal|snackbar|button|message\s+(?:reads|says)|text\s+(?:reads|says)|label|"
    r"rows?\s+(?:exists?|is\s+(?:present|created|stored|written)|has|have|with|for|in)|exactly\s+(?:one|\d+)\s+rows?|"
    r"no\s+(?:new\s+|second\s+|duplicate\s+)?rows?|the\s+row\s+(?:exists|has|shows)|query|SELECT|"
    r"stored|persisted|saved|in\s+the\s+(?:database|table)|column|"
    r"log\s+(?:line|entry|event)|logs?\s+(?:show|contain|has|have)|in\s+the\s+(?:API\s+|server\s+)?logs|logged|"
    r"Datadog|Sentry|Grafana|Loki|console|event\s+`?\w+|PostHog|Amplitude|Mixpanel|Segment|Crashlytics|Firebase|"
    r"Heap|Slack|capture(?:Exception|Message)|recordException|logEvent|"
    r"emails?|notification|redirect(?:s|ed)?|URL|navigates?|download(?:s|ed)?|file\s+(?:exists|contains))\b",
    re.I,
)

# Fallbacks that, after an existence check, only a concurrent write reaches.
_RACE_ONLY_RE = re.compile(
    r"\b(?:23505|UNIQUE_VIOLATION|unique[- ]violation|duplicate[- ]key|ER_DUP_ENTRY|P2002|E11000|40001|"
    r"40P01|serialization\s+failure|deadlock|version\s+conflict|optimistic\s+lock\w*)\b",
    re.I,
)
_CONCURRENT_RE = re.compile(
    r"\b(?:concurrent(?:ly)?|simultaneous(?:ly)?|at\s+the\s+same\s+time|in\s+parallel|parallel|race|"
    r"two\s+(?:tabs|windows|devices|sessions|requests|clients)|both\s+(?:requests|tabs)\s+at\s+once|"
    r"Promise\.all|xargs\s+-P|&\s*$)",
    re.I,
)

_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'`(]|[a-z]+[A-Z_]\w*\b)")
# Clause boundaries inside a sentence; the separator is kept so text can be put back together.
# A bare " and " is not a boundary: "the race between the RPC and the upsert" is one noun phrase.
_CLAUSE_RE = re.compile(r"(;\s+|:\s+|\s+—\s+|,\s+(?:and\s+|then\s+|but\s+|so\s+|because\s+|since\s+|as\s+)|"
                        r"\s+(?:and\s+then|because|since|so\s+that)\s+(?=[A-Za-z`'\"(]))")
_LEAD_RE = re.compile(r"^(?:and|then|but|so|because|since|as)\s+", re.I)
# Labels the re-check puts in front of a corrected Expected (plan_recheck.RETRACED_LABEL, BEFORE_RETRACED_LABEL).
_LABEL_RE = re.compile(r"^\s*(?:Re-traced from the code \(rule Q\):|Before this change \(rule T, re-read from the old code\):)\s*")
# Already labelled by another rule or as an assumption: not a pass condition, so left alone.
_MARKED_RE = re.compile(r"\(rule [A-Z]{1,2}\b|^\s*Unverified\b|\bUnverified\s+—|\bassumption\b", re.I)


def _code_tokens(text: str) -> set[str]:
    out = {code for m in _ERROR_CODE_RE.finditer(text)
           for code in re.findall(r"\b(?:\d{5}|\d{2}P\d{2}|P\d{4}|E11000|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b", m.group(0))}
    out |= {m.group(0).split("(")[0] for m in _FUNC_CALL_RE.finditer(text)}
    out |= {t.strip("`") for t in re.findall(_IDENT, text)}
    return {t for t in out if len(t) >= 4}


def names_mechanism(clause: str) -> str | None:
    """What internal mechanism the clause names (for the finding), or None."""
    m = _ERROR_CODE_RE.search(clause)
    if m:
        return f"error code {m.group(0).strip()}"
    m = _BRANCH_RE.search(clause)
    if m:
        return f"internal branch ({m.group(0).strip()[:60]})"
    m = _FUNC_CALL_RE.search(clause) or _CALLED_RE.search(clause)
    if m:
        return f"function call ({m.group(0).strip()[:60]})"
    return None


def is_observable(clause: str, how_to_verify: str = "") -> bool:
    """The clause itself says what a tester sees, or how_to_verify names the same code where it shows."""
    if _OBSERVABLE_RE.search(clause):
        return True
    how = how_to_verify or ""
    if how and _OBSERVABLE_RE.search(how):
        toks = _code_tokens(clause)
        return bool(toks) and any(t in how for t in toks)
    return False


def _cap(text: str) -> str:
    """Capitalise a plain first word; never an identifier (upsertStreamedText, user_id)."""
    word = text.split(" ", 1)[0]
    return text[:1].upper() + text[1:] if re.fullmatch(r"[a-z]+", word) else text


def _clauses(sentence: str) -> list[tuple[str, str]]:
    """(separator before, clause) pairs; the first separator is ''."""
    parts = _CLAUSE_RE.split(sentence)
    out = [("", parts[0])]
    for i in range(1, len(parts) - 1, 2):
        sep, c = parts[i], parts[i + 1]
        prev = out[-1][1]
        if any(prev.count(o) > prev.count(c) for o, c in ("()", "[]", "{}")) or prev.count("`") % 2:
            out[-1] = (out[-1][0], prev + sep + c)  # never split inside brackets
        else:
            out.append((sep, c))
    return [(sep, c) for sep, c in out if c.strip()]


def split_expected(text: str, how_to_verify: str = "") -> tuple[str, list[str], list[str]]:
    """Return (observable text kept, mechanism clauses moved out, what each one named)."""
    kept_sents, moved, named = [], [], []
    lead = _LABEL_RE.match(text or "")
    if lead:  # a rule Q/T correction is a pass condition like any other: check what follows its label
        kept, mv, nm = split_expected(text[lead.end():], how_to_verify)
        return (f"{lead.group(0).strip()} {kept}".strip() if kept else ""), mv, nm
    for sent in _SENT_RE.split(text or ""):
        if not sent.strip():
            continue
        if _MARKED_RE.search(sent):
            kept_sents.append(sent)
            continue
        keep: list[tuple[str, str]] = []
        for sep, clause in _clauses(sent):
            what = names_mechanism(clause)
            if what and not is_observable(clause, how_to_verify):
                c = _LEAD_RE.sub("", clause.strip()).rstrip(" .;,")
                moved.append(_cap(c) + ".")
                named.append(what)
            else:
                keep.append((sep, clause))
        if not keep:
            continue
        if len(keep) == len(_clauses(sent)):
            kept_sents.append(sent)
            continue
        first = _LEAD_RE.sub("", keep[0][1].strip())
        rebuilt = _cap(first)
        for sep, clause in keep[1:]:
            rebuilt += (sep if sep.strip() not in ("because", "since", "as") else ", ") + clause
        rebuilt = rebuilt.rstrip(" ;,:—")
        if not rebuilt.endswith((".", "!", "?")):
            rebuilt += "."
        kept_sents.append(rebuilt)
    return " ".join(kept_sents).strip(), moved, named


def _add_note(case: dict, clauses: list[str]) -> None:
    note = (case.get(NOTE_FIELD) or "").strip()
    new = " ".join(c for c in clauses if c not in note)
    if not new:
        return
    if not note:
        case[NOTE_FIELD] = f"{NOTE_LABEL} {new}"
    elif note.startswith(NOTE_LABEL):
        case[NOTE_FIELD] = f"{note} {new}"
    else:
        case[NOTE_FIELD] = f"{NOTE_LABEL} {note} {new}"


def _record(report: dict, case: dict, section: str, action: str, reason: str, claim: str) -> None:
    report.setdefault("findings", []).append({
        "section": section, "title": (case.get("title") or claim or "")[:140], "rule": "AK",
        "action": action, "reason": reason})
    case.setdefault("grounding_downgrades", []).append({"claim": f"rule AK: {claim}"[:200], "reason": reason})


def check_observable_expected(test_plan: Any, report: dict) -> int:
    """Move internal-mechanism clauses out of every Expected into the case's note. Returns cases changed."""
    n = 0
    for section in GRADED:
        for case in getattr(test_plan, section, None) or []:
            if not isinstance(case, dict) or case.get("covered_by_unit_test"):
                continue  # a unit-level case asserts the function, which is its observable
            how = case.get("how_to_verify") or ""
            changed = False
            for key in ("expected", "expected_outcome"):
                text = case.get(key)
                if not isinstance(text, str) or not text.strip() or "(rule AK)" in text:
                    continue
                kept, moved, named = split_expected(text, how)
                if not moved:
                    continue
                _add_note(case, moved)
                changed = True
                if kept:
                    case[key] = kept
                    _record(report, case, section, "marked",
                            f"{key} named an internal code path a tester cannot observe "
                            f"({'; '.join(dict.fromkeys(named))}); moved to the case's note as the reason, "
                            "not a pass condition", moved[0])
                elif key == "expected":
                    case[key] = f"{UNOBSERVABLE_MARKER} {text}"
                    case["expected_verified"] = False
                    _record(report, case, section, "marked",
                            f"the Expected only names internal code paths ({'; '.join(dict.fromkeys(named))}) "
                            "with no observable check", moved[0])
                else:
                    case.pop(key, None)
                    _record(report, case, section, "marked",
                            "expected_outcome only named an internal code path; moved to the note", moved[0])
            if changed:
                n += 1
    return n


def label_unchecked_race_paths(test_plan: Any, report: dict) -> int:
    """A note naming a race-only fallback, unconfirmed by the trace critic, on sequential steps: say so."""
    n = 0
    for section in GRADED:
        for case in getattr(test_plan, section, None) or []:
            if not isinstance(case, dict):
                continue
            note = case.get(NOTE_FIELD) or ""
            m = _RACE_ONLY_RE.search(note)
            if not m or "Unverified (rule AK)" in note or NOT_REACHED_LABEL in note:
                continue
            if (case.get("mechanism_check") or {}).get("verdict") == "consistent":
                continue
            setup = " ".join([case.get("title") or "", case.get("preconditions") or "", case.get("test_data") or "",
                              " ".join(s for s in case.get("steps") or [] if isinstance(s, str))])
            if _CONCURRENT_RE.search(setup):
                continue
            case[NOTE_FIELD] = f"{note} {RACE_UNCHECKED.format(what=f'The {m.group(0)} fallback')}".strip()
            n += 1
            _record(report, case, section, "marked",
                    f"the note names the {m.group(0)} fallback, which these sequential steps do not drive; "
                    "not re-traced", m.group(0))
    return n


def collect_path_items(test_plan: Any) -> list[tuple[str, int, dict]]:
    """Cases whose note names a code path, for the trace critic (kind 4)."""
    out = []
    for section in GRADED:
        for i, case in enumerate(getattr(test_plan, section, None) or []):
            if isinstance(case, dict) and (case.get(NOTE_FIELD) or "").strip() and \
                    NOT_REACHED_LABEL not in case[NOTE_FIELD]:
                out.append((section, i, case))
    return out


def apply_path_verdict(case: dict, section: str, v: dict, report: dict) -> None:
    """Kind 4 verdict: replace a note the steps do not reach with the path they do take."""
    verdict = v.get("verdict")
    note = case.get(NOTE_FIELD) or ""
    if verdict == "consistent":
        case["mechanism_check"] = {"verdict": "consistent", "traced_path": (v.get("traced_path") or "").strip()}
        return
    if verdict != "contradicted":
        case["mechanism_check"] = {"verdict": "unverifiable", "reason": (v.get("reason") or "").strip()}
        return
    actual = (v.get("actual") or v.get("reason") or "").strip() or "the steps take a different path"
    only = (v.get("reached_only_under") or "").strip().lower()
    old = note[len(NOTE_LABEL):].strip() if note.startswith(NOTE_LABEL) else note
    new = f"{NOTE_LABEL} {actual}"
    if only in ("race", "fault"):
        what = "a race (another write landing between the check and the write)" if only == "race" else "a fault"
        new += f" {NOT_REACHED_LABEL} the path named before (“{old}”) runs only under {what}."
    else:
        new += f" {NOT_REACHED_LABEL} “{old}”."
    case[NOTE_FIELD] = new
    case["mechanism_check"] = {"verdict": "contradicted", "actual": actual, "reached_only_under": only or None,
                               "traced_path": (v.get("traced_path") or "").strip(), "original_note": note}
    _record(report, case, section, "marked",
            f"the note named a code path these steps do not take{' (only under a ' + only + ')' if only else ''}; "
            f"replaced with the traced path: {actual[:200]}", old[:120])
