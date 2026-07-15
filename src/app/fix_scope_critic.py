"""Post-generation critic that checks whether each test case exercises
behaviour the merged PR actually changed.

The generator sometimes emits cases that echo a reporter's diagnostic aside
from the ticket description — a concern the fix explicitly did NOT address —
while still citing a real AC. Example: SK-2373's ticket text raised "does
the 3.11% FRED default rate make sense?", the merged PR body said "Tooltip
copy only (no change to the displayed balance or any FRED behavior)", and
the generator still produced an edge-case titled "Freddie Mac default rate
is NOT auto-applied…". That case cites real ACs but tests behaviour the
merged fix left alone; QA sees a failure that isn't a defect.

This module runs a lightweight verification pass after the LLM returns the
plan. Each case (title, steps, expected, cited ACs) is paired with a
snapshot of what the merged PR(s) actually did — PR title, body, files
changed, key diffs, and commit messages — and an LLM-side critic returns a
supported/unsupported verdict per case. Unsupported cases get badged
(``needs_manual_verification=True``) and gain a matching entry in the
plan's ``grounding_warnings`` so the UI surfaces them under the existing
"Ungrounded UI ref" badge, letting QA skip them.

This is a sibling of ``grounding_critic``:
  - ``grounding_critic`` checks the case against the AC text it cites
    (catches hallucinated capabilities: "filter by date range" cited to an
    AC that says "viewable").
  - ``fix_scope_critic`` (this module) checks the case against the PR's
    actual scope (catches reporter-drift: cases that test something the
    merged PR did not change).

Both use the same badge on the frontend so QA has one visual signal.
"""

from __future__ import annotations

from typing import Iterable


_MAX_STEPS_IN_CRITIC_INPUT = 8
_MAX_STEP_LEN = 240
_MAX_PR_BODY_CHARS = 2000
_MAX_COMMITS = 15
_MAX_COMMIT_LEN = 240
_MAX_FILES = 25
_MAX_PATCH_PER_FILE = 3000
_MAX_PATCH_TOTAL = 12000


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


def _looks_like_test_file(filename: str) -> bool:
    if not filename:
        return False
    lower = filename.lower()
    return (
        ".test." in lower
        or ".spec." in lower
        or lower.endswith("_test.py")
        or lower.endswith("_test.go")
        or "/tests/" in lower
        or "/__tests__/" in lower
    )


def _render_pr_scope(pr: dict) -> tuple[list[str], bool]:
    """Render a single PR's scope snippet. Returns (lines, has_signal).

    ``has_signal`` is True only when the PR contributed something the critic
    can reason about (body, files, commits, or patches). A bare PR with just
    a title carries no scope signal — the caller should treat it as if
    absent.
    """
    lines: list[str] = []
    has_signal = False

    title = (pr.get("title") or "").strip()
    status = (pr.get("status") or "").strip()
    body = (pr.get("github_description") or "").strip()
    files = pr.get("files_changed") or []

    if title:
        lines.append(f"PR TITLE: {title}")
    if status:
        lines.append(f"PR STATUS: {status}")

    if body:
        lines.append("PR BODY:")
        lines.append(_truncate(body, _MAX_PR_BODY_CHARS))
        has_signal = True

    file_dicts = [f for f in files if isinstance(f, dict)]
    if file_dicts:
        lines.append("")
        lines.append(f"FILES MODIFIED ({len(file_dicts)}):")
        for fc in file_dicts[:_MAX_FILES]:
            fn = fc.get("filename", "?")
            st = fc.get("status", "?")
            adds = fc.get("additions", 0) or 0
            dels = fc.get("deletions", 0) or 0
            marker = "[TEST]" if _looks_like_test_file(fn) else "      "
            lines.append(f"  {marker} {st} {fn} (+{adds}/-{dels})")
        if len(file_dicts) > _MAX_FILES:
            lines.append(f"  … and {len(file_dicts) - _MAX_FILES} more")
        has_signal = True

    # Patches — highest-signal snippet of what the fix actually altered.
    patched = [f for f in file_dicts if f.get("patch")]
    if patched:
        lines.append("")
        lines.append("KEY DIFFS:")
        total = 0
        for fc in patched:
            if total >= _MAX_PATCH_TOTAL:
                lines.append("  … (remaining diffs truncated)")
                break
            patch = fc.get("patch", "") or ""
            if len(patch) > _MAX_PATCH_PER_FILE:
                patch = patch[:_MAX_PATCH_PER_FILE] + "…"
            remaining = _MAX_PATCH_TOTAL - total
            if len(patch) > remaining:
                patch = patch[:remaining] + "…"
            lines.append(f"--- {fc.get('filename', '?')} ---")
            lines.append(patch)
            total += len(patch)
        has_signal = True

    return lines, has_signal


def _render_ticket_scope(ticket_key: str, dev_info: dict | None) -> str:
    """Render the merged-PR context for one ticket. Empty string when there
    is no usable signal (missing dev_info, no PRs, no commits/patches/body)."""
    if not isinstance(dev_info, dict):
        return ""
    prs = dev_info.get("pull_requests") or []
    commits = dev_info.get("commits") or []

    all_lines: list[str] = []
    has_signal = False

    for pr in prs:
        if not isinstance(pr, dict):
            continue
        pr_lines, pr_had_signal = _render_pr_scope(pr)
        if pr_lines:
            all_lines.extend(pr_lines)
            all_lines.append("")
        has_signal = has_signal or pr_had_signal

    commit_dicts = [c for c in commits if isinstance(c, dict)]
    if commit_dicts:
        all_lines.append(f"COMMITS ({len(commit_dicts)}):")
        for c in commit_dicts[:_MAX_COMMITS]:
            # Keep the subject + first body para. Bodies often carry
            # "no change to X" phrasing that titles omit.
            msg = (c.get("message") or "").strip()
            msg = _truncate(msg, _MAX_COMMIT_LEN)
            all_lines.append(f"- {msg}")
        if len(commit_dicts) > _MAX_COMMITS:
            all_lines.append(f"… and {len(commit_dicts) - _MAX_COMMITS} more commits")
        has_signal = True

    if not has_signal:
        return ""
    header = f"━━━ {ticket_key} ━━━"
    return header + "\n" + "\n".join(all_lines).rstrip()


def build_fix_scope_summary(dev_infos: list[dict]) -> str:
    """Render the merged-PR context across all tickets in one string.

    ``dev_infos`` is a list of ``{"ticket_key": str, "development_info":
    dict | None}`` entries. Returns an empty string when NO ticket has
    usable scope signal — the caller should skip the critic in that case
    rather than send an empty prompt.
    """
    sections: list[str] = []
    for entry in dev_infos or []:
        if not isinstance(entry, dict):
            continue
        ticket_key = (entry.get("ticket_key") or "").strip() or "?"
        section = _render_ticket_scope(ticket_key, entry.get("development_info"))
        if section:
            sections.append(section)
    return "\n\n".join(sections)


def build_case_scope_inputs(test_plan) -> list[dict]:
    """Assemble the case payload the fix-scope critic will check.

    A case is included ONLY when it cites at least one AC ID — the
    ``grounding_warnings`` entry we'd append on an unsupported verdict
    needs an ``ac_id`` to attach to (matching the existing critic's
    convention).

    Cases already badged ``needs_manual_verification=True`` (either by the
    generator or by the AC-grounding critic) are skipped — the badge is
    already there and re-checking wastes budget.
    """
    out: list[dict] = []
    for section, idx, case in _iter_case_sections(test_plan):
        if case.get("needs_manual_verification"):
            continue

        raw_ids = case.get("covers_acs") or []
        ac_ids: list[str] = []
        for ac in raw_ids:
            if isinstance(ac, str) and ac.strip():
                ac_ids.append(ac.strip())
        if not ac_ids:
            continue

        title = (case.get("title") or "").strip()
        steps_raw = case.get("steps") or []
        steps: list[str] = []
        for step in steps_raw[:_MAX_STEPS_IN_CRITIC_INPUT]:
            if isinstance(step, str) and step.strip():
                steps.append(_truncate(step.strip(), _MAX_STEP_LEN))

        if not title and not steps:
            continue

        out.append({
            "case_id": _case_id(section, idx),
            "title": title,
            "steps": steps,
            "expected": _truncate((case.get("expected") or "").strip(), _MAX_STEP_LEN),
            "covers_acs": ac_ids,
        })
    return out


SCOPE_CRITIC_SYSTEM_PROMPT = """You verify whether QA test cases exercise behaviour the merged pull request(s) actually changed.

You receive:
  1. A snapshot of what the PR changed — its title, body/description, the files it modified, the diffs it introduced, and its commit messages.
  2. A list of test cases, each with title, steps, and expected outcome.

For each case, decide whether the behaviour under test is something the PR actually addressed.

**Supported** — the case exercises behaviour that the PR's code changes, added tests, or PR body claim to modify or introduce. Paraphrasing is fine; the case doesn't have to match the diff word-for-word. If a hunk touches the same feature area or the PR body describes the change, that is supported.

**Unsupported** — the case tests behaviour that is out of the PR's scope. Positive evidence includes:
  - The PR body explicitly says the behaviour was NOT changed ("no change to X", "tooltip copy only", "X unchanged", "does not touch the calculation").
  - The PR body explicitly defers the behaviour ("deferred to a follow-up", "not addressing Y in this PR", "will be handled separately", "out of scope for this ticket").
  - The case echoes a diagnostic aside from the reporter's original bug description (e.g. "does this default value make sense?", "is this behaviour still correct?") but no diff hunk, added test, or PR body statement targets it.

Be conservative — only mark **unsupported** when you can point at positive PR-side evidence that the case is out of scope. If in doubt, mark **supported**. False negatives (missed reporter-drift cases) are safer than false positives (real cases badged as skip-me).

Reply by calling the `report_scope` tool with one entry per case. The `reason` field must be one short sentence: for **unsupported**, name the specific PR-side evidence (e.g. "PR body: 'tooltip copy only, no change to FRED behavior'"); for **supported**, name the diff, test, or PR-body statement that anchors the case."""


REPORT_SCOPE_TOOL = {
    "name": "report_scope",
    "description": "Return a supported/unsupported verdict for each test case against the PR scope.",
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
                            "enum": ["supported", "unsupported"],
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


def build_scope_critic_user_message(cases: list[dict], fix_scope: str) -> str:
    """Render the case-verification payload as the user-message string.

    Deterministic (no timestamps, stable ordering) so tests can assert
    against it directly.
    """
    lines: list[str] = [
        "Verify each of the following test cases against the merged PR scope described below.",
        "Call the `report_scope` tool with one verdict per case.",
        "",
        "══════════ MERGED PR SCOPE ══════════",
        fix_scope.strip(),
        "═════════════════════════════════════",
        "",
        "══════════ TEST CASES TO VERIFY ══════════",
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
        if case.get("covers_acs"):
            lines.append(f"Cites ACs: {', '.join(case['covers_acs'])}")
        lines.append("")
    return "\n".join(lines)


def apply_scope_verdicts(
    test_plan,
    verdicts: dict[str, dict],
) -> list[dict]:
    """Mutate the plan in place: badge unsupported cases and record warnings.

    ``verdicts`` maps ``case_id`` (e.g. ``"edge_cases:0"``) to
    ``{"verdict": "supported"|"unsupported", "reason": str}``.

    For each unsupported case:
      1. Set ``needs_manual_verification=True`` so the frontend renders
         the existing "Ungrounded UI ref" badge.
      2. Append a ``grounding_warnings`` entry with ``ac_id`` (first cited
         AC), ``missing_element`` (case title), and ``explanation``
         prefixed "Fix-scope critic: …" so operators can tell this warning
         came from the scope pass, not the AC-grounding pass.

    Returns the list of new warning entries appended.
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
        if verdict.get("verdict") != "unsupported":
            continue

        covers_acs = case.get("covers_acs") or []
        first_ac = next(
            (a for a in covers_acs if isinstance(a, str) and a.strip()),
            "",
        )
        if not first_ac:
            continue

        case["needs_manual_verification"] = True

        reason = (verdict.get("reason") or "").strip() or (
            "The merged PR did not change the behaviour this test asserts."
        )
        title = (case.get("title") or "").strip() or "unnamed test case"
        warning = {
            "ac_id": first_ac,
            "missing_element": title,
            "explanation": f"Fix-scope critic: {reason}",
            "source": "critic_scope",
            "severity": "warn",
        }
        existing_warnings.append(warning)
        added.append(warning)

    if added:
        test_plan.grounding_warnings = existing_warnings

    return added


def parse_scope_verdicts(raw: object) -> dict[str, dict]:
    """Coerce the LLM's tool output into ``{case_id: {verdict, reason}}``.

    Accepts either the tool-input dict (``{"verdicts": [...]}``) or the
    inner list directly. Malformed entries are dropped rather than raising
    — if the critic misbehaves we'd rather ship the plan un-badged than
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
        if verdict not in ("supported", "unsupported"):
            continue
        reason = entry.get("reason")
        out[cid.strip()] = {
            "verdict": verdict,
            "reason": reason.strip() if isinstance(reason, str) else "",
        }
    return out
