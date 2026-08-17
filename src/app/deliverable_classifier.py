"""Pre-plan classifier that names the ticket's deliverable and the surface
where the change is observable.

The generator historically assumes the "system under test" is the running
app. That default breaks on tickets like SK-2546 ("Pilot App: Update App
Store Images"), where the deliverable is a set of screenshots uploaded
to App Store Connect and the surface where you observe the change is
the live App Store listing — NOT the app itself. When the generator
inherits the wrong target, every downstream case inherits it too: on
SK-2546 the plan told QA to compare the Drive PNG against the running
app's "Popular Calculators" section, which is a designer-fidelity check,
not a verification of the actual work item.

This module runs one lightweight LLM call BEFORE plan generation and
returns:

  - ``artifact_type`` — a coarse label ("app_store_asset",
    "code_behavior", "config_flag", "content_copy", "documentation",
    "infrastructure", "dependency_bump", "unknown"). Downstream code
    can key off this to route to templates or heuristics.
  - ``deliverable`` — one short sentence naming what changes.
  - ``verification_surface`` — one short sentence naming where the
    change becomes observable to a tester.
  - ``anchor_steps`` — 1-3 imperative bullets a plan should include.
  - ``off_target_signals`` — 1-3 bullets naming things a plan should NOT
    do for this ticket (e.g. "do not launch the app to check
    'Popular Calculators' — no runtime code changed").

The classifier output is:
  1. Injected into the generator prompt as a "DELIVERABLE CONTEXT"
     block so the generator anchors cases to the correct surface.
  2. Fed to the sibling ``surface_mismatch_critic`` after generation
     so cases that drift off-surface get badged and skipped in the UI.

Failures are non-fatal — with no classification the pipeline behaves
exactly as it does today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ARTIFACT_TYPES: tuple[str, ...] = (
    "code_behavior",       # runtime app code / UI / API changes
    "app_store_asset",     # App Store or Play Store screenshots, metadata, listing copy
    "config_flag",         # LaunchDarkly / env / feature flag flip
    "content_copy",        # microcopy, marketing site text, localization strings
    "documentation",       # README / runbook / help center
    "infrastructure",      # deploy targets, IaC, CI pipeline
    "dependency_bump",     # library upgrades with no product surface change
    "unknown",             # classifier couldn't decide — pipeline should fall back to default
)


@dataclass
class Deliverable:
    """Structured classifier output. Kept intentionally small so it can
    be threaded through ``testing_context["deliverable_hint"]`` without
    signature changes across every provider."""

    artifact_type: str
    deliverable: str
    verification_surface: str
    anchor_steps: list[str] = field(default_factory=list)
    off_target_signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "deliverable": self.deliverable,
            "verification_surface": self.verification_surface,
            "anchor_steps": list(self.anchor_steps),
            "off_target_signals": list(self.off_target_signals),
        }


CLASSIFY_DELIVERABLE_SYSTEM_PROMPT = """You are a QA lead classifying a Jira ticket so a test plan can anchor to the right verification surface.

Given a ticket's summary, description, and any linked-PR / development info, name TWO things:

  1. **Deliverable** — what artifact does this ticket change? One short sentence.
     Examples:
       - "New screenshot uploaded as the first slide of the Pilot app's App Store listing."
       - "A `flex-1` class added to the account-screen name wrapper in apps/expo."
       - "LaunchDarkly flag `pilot-uat-2026` set to 100% for prod."
       - "README section on local setup rewritten to reflect the new install script."

  2. **Verification surface** — where does that change become observable to a tester? One short sentence.
     Examples:
       - "App Store Connect Media Manager for the Pilot app, and the live App Store product page after review."
       - "Account screen in a local iOS Simulator dev build with a long-name user."
       - "The LaunchDarkly dashboard, and any client the flag targets after a fresh evaluation."
       - "The rendered README on GitHub and a fresh clone-and-install run."

  **CRITICAL: the surface must match the artifact.**
    - If the deliverable lives OUTSIDE the running app (an image upload, a flag flip, a doc edit,
      a marketing site copy change), the surface is NOT "the running app". Do not default to it.
    - If the deliverable is a code change with runtime effect, the surface IS the running app
      (or its API), and you should say which screen / endpoint / component.

Also produce:

  3. **artifact_type** — one of: code_behavior, app_store_asset, config_flag, content_copy,
     documentation, infrastructure, dependency_bump, unknown. Use `unknown` only when the ticket
     is genuinely ambiguous — do not use it to hedge.

  4. **anchor_steps** — 1 to 3 short imperative bullets a test plan MUST include to actually
     verify the deliverable on the surface you named. Skip generic setup; name concrete places.
     Example for an app_store_asset ticket:
       - "Open App Store Connect → Pilot app → Screenshots and confirm the first slide is the updated file."
       - "After metadata submission, open the App Store on an iOS device and confirm the first screenshot matches."

  5. **off_target_signals** — 1 to 3 short bullets naming things a plan should NOT do because
     they don't verify THIS ticket. Cite the reason briefly. Example for app_store_asset:
       - "Do not launch the app to check the 'Popular Calculators' section — no runtime code changed."
       - "Do not compare the Drive PNG to the live app — that's a designer-fidelity check, not this ticket."

Reply by calling the `report_deliverable` tool exactly once. Keep every field short and specific."""


REPORT_DELIVERABLE_TOOL = {
    "name": "report_deliverable",
    "description": "Return the classified deliverable, verification surface, and anchoring hints for the ticket.",
    "input_schema": {
        "type": "object",
        "properties": {
            "artifact_type": {
                "type": "string",
                "enum": list(ARTIFACT_TYPES),
            },
            "deliverable": {"type": "string"},
            "verification_surface": {"type": "string"},
            "anchor_steps": {
                "type": "array",
                "items": {"type": "string"},
            },
            "off_target_signals": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "artifact_type",
            "deliverable",
            "verification_surface",
            "anchor_steps",
            "off_target_signals",
        ],
    },
}


_MAX_DESC_CHARS = 4000
_MAX_PR_TITLES = 8
_MAX_FILE_LIST = 20


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def build_classifier_user_message(
    ticket_key: str,
    summary: str,
    description: str,
    issue_type: str | None = None,
    development_info: dict | None = None,
) -> str:
    """Render the classifier's user message. Deterministic so tests can
    assert against it directly."""
    lines: list[str] = [
        f"TICKET: {ticket_key}",
        f"TYPE: {issue_type or 'unknown'}",
        f"SUMMARY: {summary or ''}",
        "",
        "DESCRIPTION:",
        _truncate(description or "", _MAX_DESC_CHARS) or "(none)",
        "",
    ]

    prs = (development_info or {}).get("pull_requests") or []
    pr_titles: list[str] = []
    file_paths: list[str] = []
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        title = (pr.get("title") or "").strip()
        if title:
            pr_titles.append(title)
        for fc in (pr.get("files_changed") or [])[: _MAX_FILE_LIST]:
            if isinstance(fc, dict):
                fn = fc.get("filename")
                if isinstance(fn, str) and fn.strip():
                    file_paths.append(fn.strip())

    if pr_titles:
        lines.append("LINKED PR TITLES:")
        for t in pr_titles[:_MAX_PR_TITLES]:
            lines.append(f"  - {t}")
        lines.append("")

    if file_paths:
        lines.append("FILES CHANGED (from linked PRs):")
        for fn in file_paths[:_MAX_FILE_LIST]:
            lines.append(f"  - {fn}")
        if len(file_paths) > _MAX_FILE_LIST:
            lines.append(f"  … and {len(file_paths) - _MAX_FILE_LIST} more")
        lines.append("")
    else:
        # Explicitly telling the classifier "no code changed" is a strong
        # signal — the SK-2546 shape (asset upload with no repo path) is
        # exactly this.
        lines.append("FILES CHANGED (from linked PRs): none")
        lines.append("")

    lines.append(
        "Call the `report_deliverable` tool with your classification. "
        "Remember: if no code changed, the verification surface is NOT the running app."
    )
    return "\n".join(lines)


def parse_deliverable(raw: object) -> Deliverable | None:
    """Coerce the tool input into a ``Deliverable``. Returns ``None`` on
    malformed input so the caller can degrade gracefully."""
    if not isinstance(raw, dict):
        return None
    artifact_type = raw.get("artifact_type")
    if artifact_type not in ARTIFACT_TYPES:
        return None
    deliverable = raw.get("deliverable")
    surface = raw.get("verification_surface")
    if not isinstance(deliverable, str) or not deliverable.strip():
        return None
    if not isinstance(surface, str) or not surface.strip():
        return None

    def _clean_list(v: object) -> list[str]:
        if not isinstance(v, list):
            return []
        out: list[str] = []
        for item in v:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    out.append(s)
        return out

    return Deliverable(
        artifact_type=artifact_type,
        deliverable=deliverable.strip(),
        verification_surface=surface.strip(),
        anchor_steps=_clean_list(raw.get("anchor_steps")),
        off_target_signals=_clean_list(raw.get("off_target_signals")),
    )


def aggregate_deliverables_for_critique(
    per_ticket: list[tuple[str, Deliverable | None]],
) -> Deliverable | None:
    """Fold per-ticket classifier outputs into a single ``Deliverable``
    the surface critic can reason against for a multi-ticket batch.

    Rules:
      - Any ``code_behavior`` deliverable in the batch → return ``None``.
        The batch mixes runtime code with non-code work; the surface
        critic would false-positive on the code ticket's legitimate
        "launch the app" cases. Skip it; the fix-scope critic still
        covers the code side.
      - No non-``unknown`` deliverables → return ``None``. Nothing to
        critique against.
      - Exactly one non-``unknown`` deliverable → return it as-is (with
        its ticket key prefixed to the surface line for clarity in the
        critic prompt).
      - Multiple non-``unknown`` non-code deliverables → return a
        synthetic ``Deliverable`` whose ``verification_surface``
        concatenates each ticket's surface (prefixed with its key) and
        whose lists are the deduped union. ``artifact_type`` is set to
        the most common type across the batch (ties broken by first
        seen) so the badge tag is informative.

    Ticket keys are threaded in so the critic can name which surface a
    case is off from — otherwise a mixed batch collapses to opaque
    "off_surface" verdicts.
    """
    usable: list[tuple[str, Deliverable]] = [
        (k, d) for k, d in per_ticket if d is not None and d.artifact_type != "unknown"
    ]
    if not usable:
        return None
    if any(d.artifact_type == "code_behavior" for _, d in usable):
        return None
    if len(usable) == 1:
        key, d = usable[0]
        return Deliverable(
            artifact_type=d.artifact_type,
            deliverable=f"({key}) {d.deliverable}",
            verification_surface=f"({key}) {d.verification_surface}",
            anchor_steps=[f"({key}) {s}" for s in d.anchor_steps],
            off_target_signals=[f"({key}) {s}" for s in d.off_target_signals],
        )

    # Multi-deliverable synthesis.
    type_counts: dict[str, int] = {}
    for _, d in usable:
        type_counts[d.artifact_type] = type_counts.get(d.artifact_type, 0) + 1
    dominant_type = max(type_counts.items(), key=lambda kv: kv[1])[0]

    deliverable_lines = [f"({k}) {d.deliverable}" for k, d in usable]
    surface_lines = [f"({k}) {d.verification_surface}" for k, d in usable]

    anchor_steps: list[str] = []
    seen_anchors: set[str] = set()
    for k, d in usable:
        for s in d.anchor_steps:
            tagged = f"({k}) {s}"
            if tagged in seen_anchors:
                continue
            seen_anchors.add(tagged)
            anchor_steps.append(tagged)

    off_target_signals: list[str] = []
    seen_off: set[str] = set()
    for k, d in usable:
        for s in d.off_target_signals:
            tagged = f"({k}) {s}"
            if tagged in seen_off:
                continue
            seen_off.add(tagged)
            off_target_signals.append(tagged)

    return Deliverable(
        artifact_type=dominant_type,
        deliverable=" | ".join(deliverable_lines),
        verification_surface=" | ".join(surface_lines),
        anchor_steps=anchor_steps,
        off_target_signals=off_target_signals,
    )


def format_deliverable_hint(d: Deliverable) -> str:
    """Render the classifier output as a prompt block for the generator.

    The generator's ``_build_prompt`` pulls this out of
    ``testing_context["deliverable_hint"]`` and injects it verbatim — so
    the phrasing here is what steers case authorship. Keep it directive.
    """
    lines: list[str] = [
        "**Deliverable context (classifier output — treat as ground truth for anchoring test cases):**",
        f"- Artifact type: `{d.artifact_type}`",
        f"- Deliverable: {d.deliverable}",
        f"- Verification surface: {d.verification_surface}",
    ]
    if d.anchor_steps:
        lines.append("- Anchor every case in this plan to that surface. Cases MUST include:")
        for s in d.anchor_steps:
            lines.append(f"  * {s}")
    if d.off_target_signals:
        lines.append("- Do NOT include cases that:")
        for s in d.off_target_signals:
            lines.append(f"  * {s}")
    if d.artifact_type != "code_behavior":
        lines.append(
            "- This ticket does not change runtime code. "
            "Do not write cases that launch the app to verify behaviour — "
            "the verification surface above is where the change is observable."
        )
    return "\n".join(lines)
