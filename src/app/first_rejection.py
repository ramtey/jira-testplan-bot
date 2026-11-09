"""An exact error message in Expected must come from the FIRST check that rejects that input.

On SK-2825 (plan 673) two security cases quoted error messages read from one
function in isolation:

* a table row that set the outer ``method`` to PUT expected the envelope-binding
  message ("Outer envelope field 'method' does not match…"), but the delivery
  entry point checks the HTTP-method allowlist first and rejects PUT with
  "HTTP method 'PUT' is not allowed…" before decryption ever runs;
* rows with ``[::1]`` and ``[::ffff:10.0.0.1]`` expected the address policy's
  "…is not a public address", but URL encoding runs first on the delivery path
  and rejects them as "Destination URL is not a valid absolute URL".

The product was right and the plan was wrong, so a tester would fail a correct
build. The prompt (rule Q, "first rejecting check") asks the generator to
trace every input — every table row — from the real entry point in execution
order and to record that trace in ``rejection_traces``. This module is the
half that does not trust it, run before the plan is saved:

* every exact message, HTTP status or error class pinned in a case's Expected
  must be backed by a traced path that starts at the entry point (two or more
  ``<file>:<line>`` hops) and names the check that rejects first;
* a table-driven case needs one trace per row, and each row's first message
  must be what Expected says for it — one message applied to the whole table
  is not a trace;
* when the source is in the index, the cited first check must contain the
  message (allowing interpolated values); unread source never flags.

A pin that fails is downgraded to the outcome the spec requires
(``expected_outcome`` when the generator wrote one), with the predicted wording
kept as "record what is shown", and listed in the plan's re-read line.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

MARKER = "Exact message unverified (rule Q, first rejecting check): {why} —"
OUTCOME_ONLY = ("pass or fail on the outcome the spec requires, not on this wording; record the exact "
                "message, status and error shown.")

_FILE_LINE = re.compile(r"[\w./-]+\.[A-Za-z]{1,6}:\d+(?:-\d+)?")
# Minimum length 1 so quote pairs stay aligned: with a minimum of 6, the
# closing backtick of `exp` opened a "quote" that ran to the next code span.
_QUOTE = re.compile(r"\"([^\"]{1,240})\"|“([^”]{1,240})”|`([^`]{1,240})`|(?<![\w])'([^']{1,240})'(?![\w])")
_FAILURE_WORDS = re.compile(
    r"\b(?:not|no|invalid|must|required|denied|error|fail\w*|reject\w*|unable|cannot|can't|forbidden|"
    r"unauthori[sz]ed|expired|missing|mismatch|does not match|disallowed|blocked|too (?:long|short|large|many)|"
    r"exceed\w*|unsupported|unknown|malformed|refused|wrong|try again|couldn't|could not|unavailable)\b", re.I)
_REJECT_CONTEXT = re.compile(r"\b(?:reject\w*|error\w*|fail\w*|throws?|thrown|raises?|returns?|responds?|"
                             r"denied|refuse\w*|blocked|message)\b", re.I)
_STATUS = re.compile(r"\b(?:HTTP\s*|status(?:\s*code)?\s*(?:of\s*)?|returns?\s+(?:an?\s+)?|responds?\s+(?:with\s+)?(?:an?\s+)?)"
                     r"([45]\d\d)\b", re.I)
_ERROR_CLASS = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception))\b")
_GENERIC_CLASSES = {"Error", "Exception"}
_CLASS_CONTEXT = re.compile(r"\b(?:throws?|thrown|raises?|raised|rejects?\s+with|fails?\s+with|with\s+an?)\s+(?:an?\s+)?$", re.I)
# Text a logger, a telemetry event, a reviewer or the AC wrote is not the
# response a check gives to the tester's input.
_NOT_A_RESPONSE = re.compile(
    r"\b(?:logs?|logged|log\s+(?:line|record|message|entry)|event|events|span|metric|alert(?:\s+rule)?|"
    r"flagged|reviewer|author|comment(?:ed)?|said|says|AC'?s?|acceptance criteria|ticket|query|contains|"
    r"tests?(?:\s+rows?)?|test case|spec|describe|it|the form)\b"
    r"[^.;]{0,12}$", re.I)
# A message the case says must NOT appear is not one it pins as the result.
_NEGATED_BEFORE = re.compile(r"\b(?:never|not|no|without|instead of|rather than)\b[^.;:]{0,50}$", re.I)
_NEGATED_AFTER = re.compile(r"^[^.;]{0,30}\b(?:is|are|was)\s+not\s+(?:shown|displayed|seen|returned)|"
                            r"^[^.;]{0,30}\b(?:must|should|does|do)\s+not\s+(?:appear|show|be\s+shown)|"
                            r"^[^.;]{0,20}\bnever\b", re.I)
# Observability cases check a log line, alert rule or dashboard, not a rejection.
_OBSERVABILITY_TITLE = re.compile(r"^\s*\[(?:Log shape|Alert config|Dashboard|Notification|Smoke)\]", re.I)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?;])\s+(?=[A-Z0-9\"'`(“])", text or "") if s.strip()]


def _words(text: str) -> list[str]:
    # Placeholders only: a code brace block is not a placeholder.
    t = re.sub(r"\$\{[^}\n]*\}|\{\w*\}|<[\w-]+>|%[sd]", " ", text or "")
    return [w for w in re.findall(r"[a-z0-9]+", t.lower()) if len(w) >= 2]


def message_matches(pin: str, text: str, threshold: float = 0.6) -> bool:
    """``pin`` (as Expected quotes it) is the same message as ``text`` (a trace or a code line).

    Interpolated values (the method, the address) are absent from the code, and
    Expected sometimes cuts a message with "…", so this is word coverage, not
    equality."""
    pw = _words(pin)
    if not pw:
        return False
    tw = set(_words(text))
    return sum(1 for w in pw if w in tw) / len(pw) >= threshold


def pinned(expected: str) -> list[dict]:
    """Exact messages, statuses and error classes a case's Expected pins.

    A quoted string counts as a message only when it reads like a failure and
    its sentence is about a rejection — a quoted button label does not."""
    out: list[dict] = []
    for s in _sentences(expected):
        if "(rule Q" in s:
            continue
        if re.search(r"exact (?:message|text|wording) (?:is )?unverified|record (?:what|the message) (?:is )?shown",
                     s, re.I):
            continue  # already outcome-level, as rule Q asks when the path cannot be traced
        for m in _QUOTE.finditer(s):
            q = next((g for g in m.groups() if g is not None), "")
            if len(q) < 6 or len(q.split()) < 3 or _NOT_A_RESPONSE.search(s[:m.start()]) \
                    or _NEGATED_BEFORE.search(s[:m.start()]) or _NEGATED_AFTER.search(s[m.end():]):
                continue
            if _FAILURE_WORDS.search(q) and (_REJECT_CONTEXT.search(s) or _FAILURE_WORDS.search(s.replace(q, ""))):
                out.append({"kind": "message", "text": q.strip(" .…"), "sentence": s})
        for m in _STATUS.finditer(s):
            out.append({"kind": "status", "text": m.group(1), "sentence": s})
        for m in _ERROR_CLASS.finditer(s):
            if m.group(1) not in _GENERIC_CLASSES and _CLASS_CONTEXT.search(s[:m.start()]):
                out.append({"kind": "error_class", "text": m.group(1), "sentence": s})
    return out


def table_rows(case: dict) -> list[str]:
    """Inputs of a table-driven case: markdown table rows in test_data, else 'Repeat with' steps."""
    rows = []
    for line in (case.get("test_data") or "").splitlines():
        line = line.strip()
        if not line.startswith("|") or re.fullmatch(r"\|[\s:|-]+\|?", line):
            continue
        rows.append(line)
    if len(rows) >= 2:
        return rows[1:]  # first row is the header
    steps = [s for s in case.get("steps") or [] if isinstance(s, str)]
    repeats = [s for s in steps if re.match(r"\s*(?:repeat|again)\b", s, re.I)]
    return (["(first input)"] + repeats) if repeats else []


def _traces(case: dict) -> list[dict]:
    out = []
    for t in case.get("rejection_traces") or []:
        if isinstance(t, dict):
            out.append(t)
    return out


def _trace_text(t: dict) -> str:
    return " ".join(str(t.get(k) or "") for k in ("message", "status", "error_class"))


def _hops(text: str) -> int:
    return len(_FILE_LINE.findall(text or ""))


def _region(index, locator: str, pad: int = 3) -> str | None:
    """The text around ``<file>:<line>`` from the source index; None when unread."""
    if index is None or isinstance(index, str):
        return None
    m = re.match(r"(.+?):(\d+)(?:-(\d+))?$", (locator or "").strip().strip("`"))
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
    a, b = int(m.group(2)), int(m.group(3) or m.group(2))
    if a < 1 or a > len(lines):
        return ""
    return "\n".join(lines[max(0, a - 1 - pad):min(len(lines), b + pad)])


def _problems(case: dict, pins: list[dict], index) -> list[str]:
    """Why this case's pinned messages are not backed by a traced path (empty when they are)."""
    traces = _traces(case)
    rows = table_rows(case)
    problems: list[str] = []
    if not traces:
        tp = (case.get("traced_path") or "") + " " + ((case.get("trace_check") or {}).get("traced_path") or "")
        if len(rows) >= 2:
            return [f"a table of {len(rows)} inputs with no per-row trace — each row can be rejected by a "
                    f"different check, so one message cannot be applied to the whole table"]
        if _hops(tp) < 2:
            return ["no traced path from the entry point to the first check that rejects this input"]
        if not any(message_matches(p["text"], tp) for p in pins):
            # A path with hops that never names the message: still not a trace of THIS message.
            return ["the traced path does not name the check that produces this message"]
        return []
    if len(rows) >= 2 and len(traces) < len(rows):
        problems.append(f"{len(traces)} trace(s) for {len(rows)} table rows — every row needs its own")
    for t in traces:
        who = t.get("input") or "an input"
        if _hops(str(t.get("traced_path") or "")) < 2 or not _FILE_LINE.search(str(t.get("first_rejecting_check") or "")):
            problems.append(f"the trace for {who!s:.60} does not run from the entry point to the first rejecting "
                            f"check (<file>:<line> per hop)")
            continue
        msg = _trace_text(t).strip()
        if msg and not any(message_matches(p["text"], msg) or message_matches(msg, p["text"]) for p in pins):
            problems.append(f"{who!s:.60} is rejected first by {t['first_rejecting_check']} with "
                            f"\"{msg[:100]}\", which Expected does not say")
        if t.get("message"):
            region = _region(index, str(t["first_rejecting_check"]))
            if region is not None and not message_matches(str(t["message"]), region):
                problems.append(f"{t['first_rejecting_check']} does not produce \"{str(t['message'])[:100]}\"")
    for p in pins:
        if not any(message_matches(p["text"], _trace_text(t)) for t in traces):
            problems.append(f"\"{p['text'][:100]}\" is not the first rejection of any traced input")
    return problems


def _downgrade(case: dict, pins: list[dict], why: str) -> None:
    exp = case.get("expected") or ""
    marker = MARKER.format(why=why[:240])
    outcome = (case.get("expected_outcome") or "").strip()
    predicted = "; ".join(dict.fromkeys(f"\"{p['text']}\"" if p["kind"] == "message" else p["text"]
                                        for p in pins))
    if outcome:
        case["expected"] = (f"{marker} {outcome} The plan predicted {predicted}; {OUTCOME_ONLY} "
                            f"(Original expected: {exp})").strip()
    else:
        hit = {p["sentence"] for p in pins}
        case["expected"] = " ".join(f"{marker} {OUTCOME_ONLY} Predicted: {s}" if s in hit else s
                                    for s in _sentences(exp))
    case["expected_verified"] = False
    case["pinned_message_check"] = {"verdict": "downgraded", "pins": [p["text"] for p in pins], "reason": why}


def check_pinned_messages(test_plan: Any, report: dict, index=None,
                          sections: Iterable[str] = ("happy_path", "edge_cases", "integration_tests",
                                                     "security_negative_tests")) -> int:
    """Rule Q, first rejecting check. Marks in place; returns the number downgraded."""
    from .plan_recheck import _record

    n = 0
    for section in sections:
        for case in getattr(test_plan, section, None) or []:
            if not isinstance(case, dict) or "(rule Q" in (case.get("expected") or "") \
                    or _OBSERVABILITY_TITLE.search(case.get("title") or ""):
                continue
            pins = pinned(case.get("expected") or "")
            if not pins:
                continue
            problems = _problems(case, pins, index)
            if not problems:
                case["pinned_message_check"] = {"verdict": "traced", "pins": [p["text"] for p in pins]}
                continue
            why = "; ".join(dict.fromkeys(problems))
            _downgrade(case, pins, why)
            n += 1
            _record(report, case, section, "Q", "downgraded",
                    f"exact error text without a traced first rejecting check: {why}",
                    "; ".join(p["text"] for p in pins)[:160])
    return n
