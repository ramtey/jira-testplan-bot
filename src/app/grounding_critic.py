"""Post-generation critic that checks whether each test case's claim is
actually supported by the acceptance-criteria text it cites.

The generator sometimes emits cases whose ``covers_acs`` points at an AC ID
that doesn't actually describe the behaviour the case tests — e.g. a case
titled "Audit log viewer correctly filters by date range" tagged against an
AC that only says "Audit history is viewable in the admin dashboard." That
reads to QA as a scope gap when it's really a hallucination.

This module runs a lightweight verification pass after the LLM returns the
plan. Each case is paired with the verbatim text of every AC it cites, and
an LLM-side critic returns a grounded/ungrounded verdict per case. Cases
the critic marks ungrounded get badged (``needs_manual_verification=True``)
and gain a matching entry in the plan's ``grounding_warnings`` so the UI
surfaces them under the existing "Ungrounded UI ref" badge, letting QA
skip them.

The functions here are pure — the LLM call itself lives on ``LLMClient``.
Keeping the plumbing separate makes the ungrounded-detection logic
testable without touching the network.
"""

from __future__ import annotations

from typing import Iterable


_MAX_STEPS_IN_CRITIC_INPUT = 8
_MAX_STEP_LEN = 240


def build_ac_index(tickets_data: list[dict]) -> dict[str, str]:
    """Flatten every ticket's acceptance criteria into ``{"<KEY>-AC<n>": text}``.

    Mirrors how ``_compute_ac_coverage`` numbers ACs so IDs line up with the
    ``covers_acs`` field the LLM emits.
    """
    index: dict[str, str] = {}
    for ticket in tickets_data or []:
        key = ticket.get("ticket_key")
        if not key:
            continue
        acs = ticket.get("acceptance_criteria") or []
        for i, text in enumerate(acs, 1):
            if isinstance(text, str) and text.strip():
                index[f"{key}-AC{i}"] = text.strip()
    return index


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _iter_case_sections(test_plan) -> Iterable[tuple[str, int, dict]]:
    """Yield (section, index, case) for every dict-shaped case in the plan.

    Section names match the JSON keys the frontend reads
    (``happy_path`` / ``edge_cases`` / ``integration_tests``).
    """
    buckets = (
        ("happy_path", getattr(test_plan, "happy_path", None) or []),
        ("edge_cases", getattr(test_plan, "edge_cases", None) or []),
        ("integration_tests", getattr(test_plan, "integration_tests", None) or []),
    )
    for section, cases in buckets:
        for i, case in enumerate(cases):
            if isinstance(case, dict):
                yield section, i, case


def _case_id(section: str, index: int) -> str:
    return f"{section}:{index}"


def build_case_verification_inputs(
    test_plan,
    ac_index: dict[str, str],
) -> list[dict]:
    """Assemble the ``(case, cited AC text)`` payload the critic needs.

    A case is included ONLY when it cites at least one AC ID that we have
    the text for. Cases with no ``covers_acs`` are handled by the separate
    "Untraced" badge and aren't the critic's concern; cases whose cited IDs
    don't exist are handled by ``_compute_ac_coverage``'s invalid-ID cleanup.

    Existing ``needs_manual_verification=True`` cases are skipped — the LLM
    already flagged them, so re-checking is wasted budget and can't make
    the outcome worse for QA.

    Returns a list of dicts shaped for the LLM:
        {
            "case_id": "edge_cases:2",
            "title": "...",
            "steps": ["...", "..."],
            "expected": "...",
            "test_data": "...",
            "cited_acs": [{"ac_id": "SK-2290-AC15", "text": "..."}]
        }
    """
    out: list[dict] = []
    for section, idx, case in _iter_case_sections(test_plan):
        if case.get("needs_manual_verification"):
            continue

        raw_ids = case.get("covers_acs") or []
        cited: list[dict] = []
        for ac_id in raw_ids:
            if not isinstance(ac_id, str):
                continue
            trimmed = ac_id.strip()
            text = ac_index.get(trimmed)
            if text:
                cited.append({"ac_id": trimmed, "text": text})
        if not cited:
            continue

        steps_raw = case.get("steps") or []
        steps: list[str] = []
        for step in steps_raw[:_MAX_STEPS_IN_CRITIC_INPUT]:
            if isinstance(step, str) and step.strip():
                steps.append(_truncate(step.strip(), _MAX_STEP_LEN))

        title = (case.get("title") or "").strip()
        if not title and not steps:
            # Nothing substantive to verify.
            continue

        out.append({
            "case_id": _case_id(section, idx),
            "title": title,
            "steps": steps,
            "expected": _truncate((case.get("expected") or "").strip(), _MAX_STEP_LEN),
            "test_data": _truncate((case.get("test_data") or "").strip(), _MAX_STEP_LEN),
            "cited_acs": cited,
        })
    return out


CRITIC_SYSTEM_PROMPT = """You verify whether a QA test case is grounded in the acceptance-criteria (AC) text it cites.

You receive a list of test cases. For each one you get:
  - the case's title, steps, expected outcome, and test data
  - the verbatim text of every AC ID the generator tagged in `covers_acs`

For each case, decide whether the **substantive behavioural claim** the case tests is derivable from those AC texts.

**Grounded** — the case tests a behaviour that the cited AC(s) require or clearly imply. Paraphrasing is fine; the claim doesn't have to appear word-for-word. If the AC says "the field is editable" and the case tests entering a value, that's grounded.

**Ungrounded** — the case tests a behaviour, control, filter, field, or capability that the cited AC(s) never mention and don't imply. Examples:
- AC says "history is viewable" → case tests "filters by date range" (filtering is a new capability not in the AC).
- AC says "user can edit their name" → case tests "admin can bulk-import 500 names" (different actor, different scale, not implied).
- AC says "toast appears on save" → case tests "toast can be dismissed with Escape" (dismissal UX is not implied by "appears").

Be strict — if the AC's wording doesn't contain the behavioural claim the case is testing, mark it **ungrounded**. Do NOT mark a case ungrounded just because the AC is short or vague; only mark ungrounded when the case reaches beyond what the AC actually says.

Reply by calling the `report_grounding` tool with one entry per case. The `reason` field must be one short sentence explaining the verdict (for ungrounded, name the specific claim the AC doesn't cover)."""


REPORT_GROUNDING_TOOL = {
    "name": "report_grounding",
    "description": "Return a grounded/ungrounded verdict for each verified test case.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "case_id": {"type": "string"},
                        "verdict": {
                            "type": "string",
                            "enum": ["grounded", "ungrounded"],
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["case_id", "verdict", "reason"],
                },
            },
        },
        "required": ["verdicts"],
    },
}


def build_critic_user_message(cases: list[dict]) -> str:
    """Render the case-verification payload as the user-message string.

    The output is deterministic (no timestamps, stable ordering) so tests
    can assert against it directly.
    """
    lines: list[str] = [
        "Verify each of the following test cases against its cited acceptance criteria.",
        "Call the `report_grounding` tool with one verdict per case.",
        "",
    ]
    for case in cases:
        lines.append(f"── CASE {case['case_id']} ──")
        lines.append(f"Title: {case['title']}")
        if case.get("steps"):
            lines.append("Steps:")
            for step in case["steps"]:
                lines.append(f"  - {step}")
        if case.get("expected"):
            lines.append(f"Expected: {case['expected']}")
        if case.get("test_data"):
            lines.append(f"Test data: {case['test_data']}")
        lines.append("Cited ACs:")
        for cited in case.get("cited_acs") or []:
            lines.append(f"  [{cited['ac_id']}] {cited['text']}")
        lines.append("")
    return "\n".join(lines)


def apply_verdicts(
    test_plan,
    verdicts: dict[str, dict],
) -> list[dict]:
    """Mutate the plan in place: badge ungrounded cases and record warnings.

    ``verdicts`` maps ``case_id`` (e.g. ``"edge_cases:2"``) to a dict with
    ``verdict`` (``"grounded"|"ungrounded"``) and ``reason``.

    For each ungrounded case:
      1. Set ``needs_manual_verification=True`` on the case so the frontend
         renders the existing "Ungrounded UI ref" badge.
      2. Append a ``grounding_warnings`` entry with ``ac_id`` (first cited
         AC), ``missing_element`` (case title), and ``explanation`` (critic
         reason, prefixed to make the source clear).

    Returns the list of new grounding-warning entries added (useful for
    caller-side logging).
    """
    added: list[dict] = []
    if not verdicts:
        return added

    existing_warnings = list(getattr(test_plan, "grounding_warnings", None) or [])

    for section, idx, case in _iter_case_sections(test_plan):
        cid = _case_id(section, idx)
        verdict = verdicts.get(cid)
        if not isinstance(verdict, dict):
            continue
        if verdict.get("verdict") != "ungrounded":
            continue

        covers_acs = case.get("covers_acs") or []
        first_ac = next(
            (a for a in covers_acs if isinstance(a, str) and a.strip()),
            "",
        )
        if not first_ac:
            # No AC ID left to attach the warning to; nothing the UI can render.
            continue

        case["needs_manual_verification"] = True

        reason = (verdict.get("reason") or "").strip() or (
            "The cited AC does not describe the behaviour this test asserts."
        )
        title = (case.get("title") or "").strip() or "unnamed test case"
        warning = {
            "ac_id": first_ac,
            "missing_element": title,
            "explanation": (
                f"Critic pass: {reason}"
            ),
            # Source tag is read by ``code_grounding_critic`` to decide
            # which warnings are candidates for the code-recheck pass.
            "source": "critic_ac",
            "severity": "warn",
        }
        existing_warnings.append(warning)
        added.append(warning)

    if added:
        test_plan.grounding_warnings = existing_warnings

    return added


def parse_verdicts(raw: object) -> dict[str, dict]:
    """Coerce the LLM's tool output into ``{case_id: {verdict, reason}}``.

    Accepts either the tool-input dict (``{"verdicts": [...]}``) or the
    inner list directly. Malformed entries are dropped rather than raising —
    if the critic misbehaves we'd rather ship the plan un-badged than
    fail the whole request.
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
        cid = entry.get("case_id")
        verdict = entry.get("verdict")
        if not isinstance(cid, str) or not cid.strip():
            continue
        if verdict not in ("grounded", "ungrounded"):
            continue
        reason = entry.get("reason")
        out[cid.strip()] = {
            "verdict": verdict,
            "reason": reason.strip() if isinstance(reason, str) else "",
        }
    return out
