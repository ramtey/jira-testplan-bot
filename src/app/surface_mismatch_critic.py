"""Post-generation critic that badges cases whose steps don't touch the
verification surface named by the deliverable classifier.

The classifier's hint is injected into the generator prompt, but LLMs
occasionally ignore constraints — especially on ticket types they've
been trained to write "compare against the running app" cases for. This
critic is the safety net.

Structure mirrors ``fix_scope_critic``:
  - ``build_case_surface_inputs`` — assemble the payload to verify.
  - ``build_surface_critic_user_message`` — render the user message.
  - ``parse_surface_verdicts`` — coerce tool output to a verdict dict.
  - ``apply_surface_verdicts`` — mutate the plan: badge off-surface
    cases with ``needs_manual_verification=True`` and append a
    matching ``grounding_warnings`` entry.

Only runs when the classifier returned a non-``unknown`` artifact type
AND ``code_behavior`` is NOT the result (for pure code changes the
existing three critics already cover it — this one adds nothing).
"""

from __future__ import annotations

from typing import Any, Iterable

from .deliverable_classifier import Deliverable


_MAX_STEPS_IN_CRITIC_INPUT = 8
_MAX_STEP_LEN = 240


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _iter_case_sections(test_plan) -> Iterable[tuple[str, int, dict]]:
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


def should_run(deliverable: Deliverable | None) -> bool:
    """Guard: skip the critic when it can't add signal.

    - No classifier output → nothing to compare against.
    - ``unknown`` artifact type → we don't trust the surface enough to
      badge cases against it.
    - ``code_behavior`` → the existing three critics already anchor
      cases to code / ACs / PR scope; this critic adds noise more than
      it adds signal.
    """
    if deliverable is None:
        return False
    if deliverable.artifact_type in ("unknown", "code_behavior"):
        return False
    if not (deliverable.verification_surface or "").strip():
        return False
    return True


def build_case_surface_inputs(test_plan) -> list[dict]:
    """Assemble the payload the surface critic verifies.

    Cases already badged ``needs_manual_verification=True`` are skipped
    — an earlier critic (grounding, code, fix-scope) already flagged
    them and re-checking wastes budget."""
    out: list[dict] = []
    for section, idx, case in _iter_case_sections(test_plan):
        if case.get("needs_manual_verification"):
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
        })
    return out


SURFACE_CRITIC_SYSTEM_PROMPT = """You verify whether each QA test case exercises the ticket's verification surface.

The ticket has been pre-classified. You are given:

  1. **Deliverable** — one sentence naming the artifact the ticket changes.
  2. **Verification surface** — one sentence naming where a tester observes the change.
  3. **Off-target signals** — bullets naming things a case should NOT do because they don't verify this ticket.
  4. A list of test cases (title, steps, expected).

For each case, decide:

**On-surface** — the case's steps touch the verification surface. The tester, following the case,
would actually observe the deliverable change.

**Off-surface** — the case's steps target a different surface entirely (typically "the running app"
for a ticket whose deliverable lives outside the app), OR the case matches one of the off-target
signals. Positive evidence includes:
  - The steps launch the app / open a screen / call an endpoint when the verification surface
    is App Store Connect, LaunchDarkly, a doc site, etc.
  - The case echoes an off-target signal verbatim (e.g. "verify the running app displays
    'Popular Calculators' correctly" when the ticket is an App Store screenshot upload).
  - The case verifies the *source* artifact (a Drive file, a Figma design) rather than the
    verification surface (the live App Store listing).

Be conservative — only mark **off-surface** when you can point at positive evidence in the case's
own steps or expected outcome. If a case reads as a legitimate step toward observing the
deliverable on the named surface, mark it **on-surface**. False negatives (missed drifts) are
safer than false positives (real cases badged as skip-me).

Reply by calling the `report_surface` tool with one entry per case. `reason` must be one short
sentence: for **off-surface**, name the specific step that anchors elsewhere; for **on-surface**,
name the step that touches the verification surface."""


REPORT_SURFACE_TOOL = {
    "name": "report_surface",
    "description": "Return an on-surface/off-surface verdict for each test case against the classified verification surface.",
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
                            "enum": ["on_surface", "off_surface"],
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


def build_surface_critic_user_message(cases: list[dict], deliverable: Deliverable) -> str:
    """Render the case-verification payload. Deterministic (no timestamps,
    stable ordering) so tests can assert against it directly."""
    lines: list[str] = [
        "Verify each of the following test cases against the classified verification surface.",
        "Call the `report_surface` tool with one verdict per case.",
        "",
        "══════════ DELIVERABLE CONTEXT ══════════",
        f"Artifact type: {deliverable.artifact_type}",
        f"Deliverable: {deliverable.deliverable}",
        f"Verification surface: {deliverable.verification_surface}",
    ]
    if deliverable.anchor_steps:
        lines.append("Anchor steps a well-formed plan should include:")
        for s in deliverable.anchor_steps:
            lines.append(f"  - {s}")
    if deliverable.off_target_signals:
        lines.append("Off-target signals (any case matching one of these is off-surface):")
        for s in deliverable.off_target_signals:
            lines.append(f"  - {s}")
    lines.append("═════════════════════════════════════════")
    lines.append("")
    lines.append("══════════ TEST CASES TO VERIFY ══════════")
    lines.append("")
    for case in cases:
        lines.append(f"── CASE {case['case_id']} ──")
        lines.append(f"Title: {case['title']}")
        if case.get("steps"):
            lines.append("Steps:")
            for step in case["steps"]:
                lines.append(f"  - {step}")
        if case.get("expected"):
            lines.append(f"Expected: {case['expected']}")
        lines.append("")
    return "\n".join(lines)


def parse_surface_verdicts(raw: object) -> dict[str, dict]:
    """Coerce the tool output into ``{case_id: {verdict, reason}}``.
    Malformed entries are dropped rather than raising."""
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
        if verdict not in ("on_surface", "off_surface"):
            continue
        reason = entry.get("reason")
        out[cid.strip()] = {
            "verdict": verdict,
            "reason": reason.strip() if isinstance(reason, str) else "",
        }
    return out


def apply_surface_verdicts(
    test_plan,
    verdicts: dict[str, dict],
    deliverable: Deliverable,
) -> list[dict]:
    """Mutate the plan in place: badge off-surface cases and record warnings.

    For each off-surface case:
      1. Set ``needs_manual_verification=True`` so the frontend renders
         the existing "Unverified UI" badge.
      2. Append a ``grounding_warnings`` entry with a synthetic
         ``ac_id`` ("surface:<artifact_type>") since surface warnings
         don't map to a specific AC number, ``missing_element`` (case
         title), and ``explanation`` prefixed "Surface critic: …".

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
        if verdict.get("verdict") != "off_surface":
            continue

        case["needs_manual_verification"] = True

        reason = (verdict.get("reason") or "").strip() or (
            f"Case steps do not touch the verification surface: {deliverable.verification_surface}"
        )
        title = (case.get("title") or "").strip() or "unnamed test case"
        warning = {
            "ac_id": f"surface:{deliverable.artifact_type}",
            "missing_element": title,
            "explanation": f"Surface critic: {reason}",
            "source": "critic_surface",
            "severity": "warn",
        }
        existing_warnings.append(warning)
        added.append(warning)

    if added:
        test_plan.grounding_warnings = existing_warnings

    return added
