"""Fifth-pass critic that narrows regression-checklist lines naming UI
controls the source does not have.

Why this section and not the others
-----------------------------------
The four critics before this one (:mod:`grounding_critic`,
:mod:`code_grounding_critic`, :mod:`fix_scope_critic`,
:mod:`surface_mismatch_critic`) all iterate exactly ``happy_path`` /
``edge_cases`` / ``integration_tests``. ``regression_checklist`` is the
one part of a plan that nothing looks at after generation, and the
asymmetry runs deeper than the critics:

* **The schema.** ``TEST_CASE_SCHEMA`` requires ``surface``,
  ``credentials`` and ``expected_verified`` (plus ``expected_source``
  when verified). ``regression_checklist`` is
  ``{"items": {"type": "string"}}`` — a bare string has nowhere to
  record where it was grounded.
* **The grounding rule.** ``UI_GROUNDING_GUIDANCE`` binds "every UI
  element you name in a *test step*". Checklist lines have no steps.
  Worse, its mandatory escape hatch is three case-object operations —
  write the case in a numbered section, set ``needs_manual_verification``
  in its JSON object, add a ``grounding_warnings`` entry whose ``ac_id``
  appears in that case's ``covers_acs``. None of those exist for a
  string, so a model that cannot ground a checklist line has no way to
  say so and writes the line clean.
* **The vacuity rule.** ``REGRESSION CHECKLIST RULES`` correctly bans
  "Other integrations are unaffected" and demands "which specific
  endpoint, screen, or field". Without a counterbalancing existence
  requirement that is pressure to name something concrete-sounding.

SK-2342 is the case this was written for. Two lines in plan 522 asserted
against controls that never shipped — a "Text" share option (the sheet
builds exactly PDF / "PDF & Audio Walkthrough" / "Print or Download")
and a seek control on the preview player (the progress bar is nested
plain ``View``s with ``accessibilityRole="progressbar"`` and no gesture
handler). Both passed UAT only because the runner reinterpreted them and
graded what ships. A PASS on a line naming a control that does not exist
does not mean what the plan says it means.

There is a second entry channel a prompt rule cannot close:
``seed_regressions`` imports checklist lines from Bug Lens analyses of
*sibling* tickets ("Include any that remain relevant, adapted as
needed") and those are never checked against this ticket's diff or this
repo. A post-generation pass catches both channels; a prompt constraint
catches only the lines the model writes itself.

What this critic may and may not do
-----------------------------------
**It can only narrow a line. It can never remove one.** Three
guarantees, structural rather than aspirational, because the value of
the regression checklist is breadth — it catches things *adjacent* to
the change — and a critic that emitted only what it could prove would
be a worse bug than the one it fixes:

1. ``apply_regression_verdicts`` rewrites text in place and never
   appends to or deletes from the list. The line count is invariant, so
   ``progress_key.fingerprint`` is invariant, so UAT progress recorded
   against a plan survives the pass.
2. ``unverifiable`` — no repo, no token, a code-search miss — is a
   no-op on the text. Failure to prove is never grounds to weaken a
   line; it only annotates ``regression_grounding_notes`` so a tester
   knows which lines were not checked.
3. Only lines that *name a control* are sent to the LLM at all
   (:func:`build_regression_grounding_inputs`). A flow-shaped line
   ("Navigating back from the preview screen and re-entering restores a
   playable walkthrough") names no control, is never a candidate, and
   is where adjacency coverage actually lives.

The worst outcome available to this pass is a line vaguer than it needed
to be — the direction the prompt already prefers ("A vague-but-true step
is better than a precise-but-fabricated one").

Why the source check is an LLM pass and not a testID lookup
-----------------------------------------------------------
A deterministic gate against the app's generated testID reference would
be cheaper and fully testable, and it would have been wrong. The
reference declares itself exhaustive and is not: ``agent-calculator``'s
``scripts/generate-testid-reference.js`` extracts conditional testIDs
with ``/testID=\\{([^`}][^}]*)\\}/g``, whose ``[^}]*`` stops at the first
``}`` — which inside a template literal is the one closing ``${...}``.
``testID={isLoading ? `${testID}-spinner` : `${testID}-play-pause-button`}``
therefore yields nothing, and the reference's ``DockedAudioPlayer`` entry
lists only the language and transcript buttons. A gate keyed on that
file would have cut "play/pause" — a real, shipping control — while
still missing "seek", which no testID could have refuted. Checklist
lines also name controls in prose ("the share menu", "playback
transport"), not as testIDs. So the check reads source.

Structure mirrors ``surface_mismatch_critic``:
  - ``build_regression_grounding_inputs`` — the payload to verify.
  - ``build_regression_critic_user_message`` — render the user message.
  - ``parse_regression_verdicts`` — coerce tool output to a verdict dict.
  - ``apply_regression_verdicts`` — narrow text in place, record notes.
"""

from __future__ import annotations

import re
from typing import Iterable

_MAX_LINE_LEN = 400
_MAX_SNIPPET_LEN = 3200
_MAX_FILES_IN_CRITIC_INPUT = 4
_MAX_LINES_PER_PLAN = 12

#: Leading priority emoji the renderer writes onto every checklist line.
#: Preserved verbatim across a rewrite — it feeds the frontend's severity
#: styling and is not the critic's to reassign.
_PRIORITY_EMOJI = ("🔴", "🟡", "🟢")

_VERDICTS = ("grounded", "unfounded_control", "unverifiable")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def split_priority(line: str) -> tuple[str, str]:
    """Split a checklist line into ``(priority_prefix, body)``.

    The prefix is the leading emoji plus the whitespace after it, kept
    byte-exact so a rewrite can be reassembled without the critic
    deciding a line's priority. A line with no emoji yields ``("", line)``.
    """
    stripped = line.lstrip()
    for emoji in _PRIORITY_EMOJI:
        if stripped.startswith(emoji):
            rest = stripped[len(emoji):]
            body = rest.lstrip()
            prefix = line[: len(line) - len(body)]
            return prefix, body
    return "", line


# A line is a candidate only when it names something control-shaped. The
# point is not to classify English precisely — it is to leave flow-only
# lines alone, because those are the ones carrying adjacency breadth and
# they have no control name to be wrong about.
#
# Two independent signals, either sufficient:
#
#   * a control noun ("button", "menu", "option", "toggle", …), which is
#     how a checklist line names an affordance in prose;
#   * a testID-shaped token (`share-flow-send-button`) or a parenthesised
#     control list ("(play/pause/seek)"), which is how it names one
#     literally.
#
# Deliberately NOT a candidate: "still plays English end-to-end",
# "navigating back … restores a playable walkthrough", "pre-populates the
# subject and body". Those assert behaviour, not the existence of a
# control, and a tester can grade them against whatever the app offers.
_CONTROL_NOUNS = (
    "button", "buttons", "menu", "menus", "option", "options", "control",
    "controls", "toggle", "toggles", "tab", "tabs", "link", "links",
    "icon", "icons", "field", "fields", "dropdown", "picker", "selector",
    "checkbox", "radio", "slider", "switch", "row", "rows", "sheet",
    "transport", "affordance", "affordances",
)
_CONTROL_NOUN_RE = re.compile(
    r"\b(" + "|".join(_CONTROL_NOUNS) + r")\b", re.IGNORECASE
)
#: A kebab-case token of three or more segments — the shape of this app's
#: testIDs (``share-flow-client-name-input``). Two segments would match
#: ordinary hyphenated English ("audio-language", "pre-populates").
_TESTID_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+){2,}\b")
#: Segment words that mark a three-plus-segment token as English rather than
#: a testID: "end-to-end", "day-to-day", "state-of-the-art". A testID is a
#: path through a component tree and never hinges on a function word.
_ENGLISH_HYPHEN_SEGMENTS = frozenset({
    "to", "of", "and", "or", "the", "a", "an", "in", "on", "by", "for",
    "at", "per", "vs", "non", "re",
})


def _looks_like_a_testid(token: str) -> bool:
    return not any(seg in _ENGLISH_HYPHEN_SEGMENTS for seg in token.split("-"))
#: A parenthesised slash-list, which is how a line enumerates the members
#: of one control: "(play/pause/seek)".
_SLASH_LIST_RE = re.compile(r"\([^)]*/[^)]*\)")


def names_a_control(line: str) -> bool:
    """Whether this checklist line asserts something about a named control.

    Only these lines are candidates. See the module docstring: leaving
    flow-shaped lines unexamined is what keeps this pass from narrowing
    the checklist's breadth.
    """
    _, body = split_priority(line or "")
    if not body.strip():
        return False
    if _CONTROL_NOUN_RE.search(body) or _SLASH_LIST_RE.search(body):
        return True
    return any(
        _looks_like_a_testid(m.group(0))
        for m in _TESTID_TOKEN_RE.finditer(body)
    )


def _iter_candidate_lines(test_plan) -> Iterable[tuple[int, str]]:
    """``(index, line)`` for every checklist entry naming a control.

    Indices are positions in the original ``regression_checklist`` so
    :func:`apply_regression_verdicts` can write back in place. Non-string
    entries are skipped rather than coerced — nothing in the pipeline
    produces them today, and a dict here would mean the section's shape
    changed under us.
    """
    items = getattr(test_plan, "regression_checklist", None) or []
    for i, line in enumerate(items):
        if not isinstance(line, str) or not line.strip():
            continue
        if not names_a_control(line):
            continue
        yield i, line


def line_id(index: int) -> str:
    return f"regression_checklist:{index}"


def build_search_query(line: str) -> str:
    """Build a GitHub code-search query from a checklist line.

    Same shape as ``code_grounding_critic.build_search_query`` — token
    order preserved because GitHub ranks by token relevance — but keyed
    off the line body rather than a case title, and with the priority
    emoji and control nouns dropped. The nouns are what made the line a
    candidate; they are also what every file in a UI repo contains, so
    leaving them in buries the domain terms that actually locate source.
    """
    _, body = split_priority(line or "")
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in body.split():
        token = "".join(ch for ch in raw if ch.isalnum() or ch in "_-").lower()
        if not token:
            continue
        if token in _QUERY_STOPWORDS or token in _CONTROL_NOUNS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= 6:
            break
    return " ".join(tokens)


_QUERY_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "not", "of", "to", "in", "on", "at",
    "for", "with", "by", "from", "into", "as", "is", "are", "be", "been",
    "being", "was", "were", "does", "do", "did", "when", "if", "then",
    "still", "works", "work", "same", "before", "after", "change",
    "changed", "unaffected", "behaves", "behave", "correctly", "properly",
    "successfully", "its", "it", "that", "this", "these", "those",
})


def build_regression_grounding_inputs(
    test_plan,
    hits_by_line: dict[int, list[dict]],
    testid_reference: str | None = None,
    limit: int = _MAX_LINES_PER_PLAN,
) -> list[dict]:
    """Assemble the ``(line, source snippets)`` payload for the LLM.

    ``hits_by_line`` is keyed by the line's index in
    ``regression_checklist``. Unlike the code-grounding critic, a line
    with **no** snippets is still included: "we looked and found no
    source" and "we never looked" are different facts, and the critic has
    to be able to return ``unverifiable`` for the second rather than
    silently dropping the line from the pass. The caller distinguishes
    them — it only populates ``hits_by_line`` for searches that ran.

    ``testid_reference`` is passed as *supporting* evidence only. It is
    demonstrably incomplete (see the module docstring), so the system
    prompt tells the model absence from it proves nothing.
    """
    out: list[dict] = []
    for idx, line in _iter_candidate_lines(test_plan):
        if len(out) >= limit:
            break
        prefix, body = split_priority(line)
        snippets: list[dict] = []
        for hit in (hits_by_line.get(idx) or [])[:_MAX_FILES_IN_CRITIC_INPUT]:
            if not isinstance(hit, dict):
                continue
            path = (hit.get("path") or "").strip()
            content = hit.get("content") or ""
            if not path or not content:
                continue
            snippets.append({
                "path": path,
                "ref": (hit.get("ref") or "").strip(),
                "content": _truncate(content, _MAX_SNIPPET_LEN),
            })
        out.append({
            "line_id": line_id(idx),
            "priority": prefix.strip(),
            "line": _truncate(body, _MAX_LINE_LEN),
            "source_snippets": snippets,
            "testid_reference": (testid_reference or "").strip() or None,
        })
    return out


REGRESSION_CRITIC_SYSTEM_PROMPT = """You check whether the UI controls named in a QA regression-checklist line actually exist in the app's source.

A regression checklist exists to catch breakage *adjacent* to a change. Its value is breadth, so
your job is NOT to decide whether a line is worth testing, and NOT to decide whether it is provable.
Your only job is narrower than that: **when a line names a specific control the source does not
have, produce a rewrite that keeps the line's subject and drops the phantom control.**

You are given, per line:
  1. **The line** — one regression-checklist item, priority emoji stripped.
  2. **Source snippets** — file paths and contents from the repo the ticket's PR touched. May be empty.
  3. **A testID reference** — sometimes. It is AUTO-GENERATED AND INCOMPLETE: it misses controls whose
     testID is a conditional expression. A control's ABSENCE from it is NOT evidence the control is
     missing. Its PRESENCE is evidence the control exists.

Return one verdict per line:

**grounded** — every control the line names is present in the source you were shown, OR the line
names no specific control and asserts behaviour instead. This is the default. Choose it whenever you
are not looking at positive evidence of a phantom.

**unfounded_control** — the source you were shown positively contradicts a control the line names:
you can see the exhaustive list the control would have to appear in, or the component that would
render it, and it is not there. Requires evidence of ABSENCE, not absence of evidence. Examples of
the standard:
  - The line says "Text, Print and Download share options"; the snippet builds the option array and
    it contains exactly PDF, "PDF & Audio Walkthrough", "Print or Download". The array is the
    complete list, so "Text" is contradicted. → unfounded_control
  - The line says "play/pause/seek"; the snippet is the whole player component, its progress bar is
    a plain non-interactive view, and there is no seek handler anywhere in it. → unfounded_control
  - The line names a screen you were shown no source for. → unverifiable, NOT unfounded_control.

**unverifiable** — you cannot tell from what you were shown. No snippets, snippets that don't cover
the surface the line names, or a control you simply cannot locate. This is a normal, frequent answer
and has no cost: the line is left exactly as written and merely annotated.

For **unfounded_control** you MUST also return `rewrite`: the corrected line, priority emoji omitted.
Rules for the rewrite — all of them:
  - Keep the same subject and surface. The tester must still be pointed at the same neighbouring
    area of the app. Do not turn a specific line into "the app still works".
  - Remove ONLY the control the source contradicts. Keep every named control that IS present.
    "Text, Print and Download share options still work" → "The Print or Download share option still
    works" (Text is gone, Print or Download stays).
  - If removing the phantom would leave the line thinner than it was, replace it with an observable
    the source DOES show, so the line keeps its grading power. A player with no seek still has a
    progress bar and a remaining-time label — name those instead.
  - Never introduce a control, label, string or testID that does not appear in the snippets you were
    shown. A rewrite that invents its replacement is the same bug as the line you are fixing.
  - Keep it one sentence, in the same voice as the original.

Be conservative. A phantom control left in place is a line a tester has to reinterpret; a real
control wrongly deleted is coverage silently lost, which is worse. When the snippets do not settle
it, say **unverifiable**.

Reply by calling the `report_regression_grounding` tool with one entry per line. `reason` is one
short sentence: for **unfounded_control** name the file that contradicts the control; for
**unverifiable** say what you'd need to see; for **grounded** name the control you found and where."""


REPORT_REGRESSION_GROUNDING_TOOL = {
    "name": "report_regression_grounding",
    "description": (
        "Return a grounding verdict for each regression-checklist line, with a "
        "narrowed rewrite for any line naming a control the source contradicts."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line_id": {"type": "string"},
                        "verdict": {
                            "type": "string",
                            "enum": list(_VERDICTS),
                        },
                        "reason": {"type": "string"},
                        "rewrite": {
                            "type": "string",
                            "description": (
                                "REQUIRED when verdict is unfounded_control: the corrected "
                                "line with the phantom control removed and the subject, "
                                "surface and grading power preserved. Omit the priority "
                                "emoji. Omit this field entirely for the other verdicts."
                            ),
                        },
                    },
                    "required": ["line_id", "verdict", "reason"],
                },
            },
        },
        "required": ["verdicts"],
    },
}


def build_regression_critic_user_message(lines: list[dict]) -> str:
    """Render the line-verification payload. Deterministic (no timestamps,
    stable ordering) so tests can assert against it directly."""
    out: list[str] = [
        "Check each of the following regression-checklist lines against the source below it.",
        "Call the `report_regression_grounding` tool with one verdict per line.",
        "",
    ]
    reference = next((l.get("testid_reference") for l in lines if l.get("testid_reference")), None)
    if reference:
        out += [
            "══════════ testID REFERENCE (auto-generated, INCOMPLETE) ══════════",
            "Presence here proves a control exists. Absence proves nothing.",
            "",
            reference,
            "═══════════════════════════════════════════════════════════════════",
            "",
        ]
    out.append("══════════ CHECKLIST LINES TO CHECK ══════════")
    out.append("")
    for item in lines:
        out.append(f"── LINE {item['line_id']} ──")
        out.append(f"Line: {item['line']}")
        snippets = item.get("source_snippets") or []
        if snippets:
            out.append("Source:")
            for snip in snippets:
                ref = f" @ {snip['ref']}" if snip.get("ref") else ""
                out.append(f"  ── {snip['path']}{ref} ──")
                out.append(snip["content"])
        else:
            out.append(
                "Source: none retrieved for this line — you were shown nothing about "
                "the surface it names."
            )
        out.append("")
    return "\n".join(out)


def parse_regression_verdicts(raw: object) -> dict[str, dict]:
    """Coerce the tool output into ``{line_id: {verdict, reason, rewrite}}``.

    Malformed entries are dropped rather than raising. An
    ``unfounded_control`` verdict whose ``rewrite`` is missing or blank is
    downgraded to ``unverifiable``: the only action this critic has for an
    unfounded line is to substitute the rewrite, so without one there is
    nothing to apply — and the alternative, deleting the line, is exactly
    the over-correction the pass is built not to do.
    """
    entries: object
    if isinstance(raw, dict):
        entries = raw.get("verdicts") or []
    else:
        entries = raw
    out: dict[str, dict] = {}
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        lid = entry.get("line_id")
        verdict = entry.get("verdict")
        if not isinstance(lid, str) or not lid.strip():
            continue
        if verdict not in _VERDICTS:
            continue
        reason = entry.get("reason")
        rewrite = entry.get("rewrite")
        rewrite = rewrite.strip() if isinstance(rewrite, str) else ""
        if verdict == "unfounded_control" and not rewrite:
            verdict = "unverifiable"
            reason = (
                "Critic reported an unfounded control but returned no rewrite; "
                "left as written."
            )
        out[lid.strip()] = {
            "verdict": verdict,
            "reason": reason.strip() if isinstance(reason, str) else "",
            "rewrite": rewrite if verdict == "unfounded_control" else "",
        }
    return out


def apply_regression_verdicts(test_plan, verdicts: dict[str, dict]) -> list[dict]:
    """Rewrite narrowed lines in place; annotate the rest. Returns the notes added.

    Invariants this function must preserve, and which
    ``tests/test_regression_grounding_critic.py`` pins:

    * ``len(regression_checklist)`` is unchanged. Nothing is appended,
      nothing is deleted, no line is split. The section count feeds
      ``progress_key.fingerprint``, so changing it would orphan the UAT
      progress already recorded against the plan.
    * The priority emoji of a rewritten line is carried over verbatim.
    * ``unverifiable`` and ``grounded`` never alter the text.

    Notes land on ``test_plan.regression_grounding_notes`` — a separate
    list rather than ``grounding_warnings``, because those entries are
    keyed to an ``ac_id`` and a case title and are rendered as per-case
    "Unverified UI" badges. A checklist line has neither, and a narrowed
    line is not an unverified case: it has been *corrected*, and the
    tester needs the provenance, not a badge telling them to distrust it.
    """
    added: list[dict] = []
    if not verdicts:
        return added

    items = getattr(test_plan, "regression_checklist", None)
    if not isinstance(items, list):
        return added

    notes = list(getattr(test_plan, "regression_grounding_notes", None) or [])

    for idx, line in _iter_candidate_lines(test_plan):
        verdict = verdicts.get(line_id(idx))
        if not isinstance(verdict, dict):
            continue
        kind = verdict.get("verdict")
        reason = (verdict.get("reason") or "").strip()

        if kind == "unfounded_control":
            prefix, body = split_priority(line)
            rewrite = (verdict.get("rewrite") or "").strip()
            if not rewrite:
                # parse_regression_verdicts downgrades these, so reaching
                # here means a caller hand-built the dict. Leave the line.
                continue
            items[idx] = f"{prefix}{rewrite}"
            added.append({
                "line_index": idx,
                "status": "narrowed",
                "original": body,
                "rewritten": rewrite,
                "explanation": (
                    f"Regression critic: {reason}"
                    if reason
                    else "Regression critic: named a control absent from the linked source."
                ),
                "source": "critic_regression",
                "severity": "info",
            })
        elif kind == "unverifiable":
            _, body = split_priority(line)
            added.append({
                "line_index": idx,
                "status": "unchecked",
                "original": body,
                "rewritten": "",
                "explanation": (
                    f"Regression critic: {reason}"
                    if reason
                    else "Regression critic: could not confirm this line's controls "
                         "against the linked source. Line left as written."
                ),
                "source": "critic_regression",
                "severity": "info",
            })

    if added:
        test_plan.regression_grounding_notes = notes + added

    return added
