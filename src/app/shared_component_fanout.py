"""Shared-component fan-out detection for the test-plan generator.

When a ticket adds a field to a component that fans out across multiple
calculator roles (buyer estimate + seller net sheet), a plan that tests
only the AC's positive claim ships the field to roles that shouldn't
consume it. That's the SK-1898 → SK-2511 failure mode: an assessed value
+ loan amount change to the Property Tax Results modal was tested only
as "field renders when data exists," so the misplaced loan amount on
the buyer side survived to production.

This module owns two responsibilities:

1. Detect when a ticket implicates a shared component AND is silent
   about which role(s) the change targets. See :func:`detect_fanout`.

2. Render a prompt block instructing the LLM to fan the plan out to
   every consuming role, with per-role negative-space assertions for
   fields the role does NOT consume. See :func:`render_fanout_guidance`.

The consumption map (``ROLE_CONSUMPTION_MAP``) is a hand-curated table
seeded from what the agent-calculator repo's calculators actually read.
Fields the map doesn't know about are still fanned out on, but their
negative-space check is emitted diagnostically ("capture the current
behaviour and confirm with PM") rather than as a hard assertion — that
avoids baking the current app output in as the pass criterion for a
role/field pair whose spec is genuinely undefined.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .description_analyzer import extract_acceptance_criteria


# ── Configuration ────────────────────────────────────────────────────────────

# PR file-path prefixes that mark shared components. When a ticket's linked
# PR touches ANY file whose path starts with one of these, the ticket is a
# candidate for fan-out. Seeded from agent-calculator conventions; extend
# when a new shared surface appears.
SHARED_COMPONENT_DIRS: tuple[str, ...] = (
    "apps/expo/src/components/modals/",
    "apps/expo/src/components/shared/",
    "packages/engine/src/components/shared/",
    "packages/ui/src/",
)


# Individual filename patterns that indicate a shared component regardless of
# where they live (e.g. ``*.shared.tsx``).
SHARED_COMPONENT_FILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\.shared\.(?:ts|tsx|js|jsx)$", re.IGNORECASE),
)


# Roles whose calculators feed the same shared component. Every fan-out
# fires against ALL of these — the plan must emit a case per role in this
# set (subject to the ticket not scoping itself to one of them first).
KNOWN_ROLES: tuple[str, ...] = ("buyer estimate", "seller net sheet")


# Static per-field consumption table. Keys are lowercase field labels as
# they'd appear in AC text; values are the subset of ``KNOWN_ROLES`` that
# consume the field. Fields NOT in this map still trigger fan-out (via the
# shared-component-dir heuristic) but are emitted with a diagnostic
# negative-space check rather than an assertion.
#
# Source of truth: read the target repo's ``mapPropertyInfo`` (or
# equivalent) in ``packages/engine/src/calculators/buyerNetSheet.ts`` /
# ``sellerNetSheet.ts`` and add the field with the roles that reference
# it. Do NOT add a field here if the mapping is genuinely ambiguous — the
# diagnostic-fallback path is designed for that case.
ROLE_CONSUMPTION_MAP: dict[str, frozenset[str]] = {
    "loan amount":    frozenset({"seller net sheet"}),
    "assessed value": frozenset({"buyer estimate", "seller net sheet"}),
    "hoa dues":       frozenset({"buyer estimate", "seller net sheet"}),
    "mortgage balance": frozenset({"seller net sheet"}),
    "sale price":     frozenset({"buyer estimate", "seller net sheet"}),
    "down payment":   frozenset({"buyer estimate"}),
    "interest rate":  frozenset({"buyer estimate"}),
    "loan term":      frozenset({"buyer estimate"}),
    "commission":     frozenset({"seller net sheet"}),
    "payoff amount":  frozenset({"seller net sheet"}),
}


# ── Role-scoping heuristics ──────────────────────────────────────────────────

# A ticket is "role-scoped" when its summary/description/AC makes clear the
# change is meant for a single role. We treat any of these as sufficient
# evidence to SKIP the fan-out — the escape hatch from the deliverable's
# rule 5. The intent is: fan out ONLY when role is genuinely unspecified.
_ROLE_SCOPING_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Title prefix like "Seller net sheet: display X" or "Buyer estimate — …"
    re.compile(r"^\s*(?:seller\s+net\s+sheet|buyer\s+(?:estimate|net\s+sheet|calculator))\s*[:\-–—]",
               re.IGNORECASE | re.MULTILINE),
    # Explicit user-story framing that names ONE role.
    re.compile(r"\bas\s+an?\s+(?:seller|buyer)\b", re.IGNORECASE),
    # Role name qualified by a scoping noun ("seller-side", "buyer view").
    re.compile(r"\b(?:seller|buyer)[- ]side\b", re.IGNORECASE),
    re.compile(r"\bonly\s+(?:seller|buyer)s?\b", re.IGNORECASE),
    re.compile(r"\bfor\s+(?:the\s+)?(?:seller|buyer)s?\s+(?:only|calculator|net\s+sheet|estimate)\b",
               re.IGNORECASE),
)


# ── Shared-language heuristics ───────────────────────────────────────────────

_SHARED_LANGUAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bshared\s+component\b", re.IGNORECASE),
    re.compile(r"\bshared\s+modal\b", re.IGNORECASE),
    re.compile(r"\bshared\s+(?:results\s+)?view\b", re.IGNORECASE),
    re.compile(r"\bresults\s+modal\b", re.IGNORECASE),
    re.compile(r"\bresults\s+view\b", re.IGNORECASE),
)


# ── Data types ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FanoutField:
    """A field the ticket introduces into a shared component, plus the
    subset of ``KNOWN_ROLES`` that consume it per ``ROLE_CONSUMPTION_MAP``.

    An empty ``consumed_by`` set means the field is not in the map — the
    prompt renderer will emit a diagnostic negative-space check rather
    than a hard assertion for that field.
    """

    name: str
    consumed_by: frozenset[str]

    @property
    def is_mapped(self) -> bool:
        return bool(self.consumed_by)


@dataclass
class FanoutContext:
    """Everything the prompt renderer needs to emit a fan-out block."""

    roles: tuple[str, ...] = KNOWN_ROLES
    fields: list[FanoutField] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    shared_paths: list[str] = field(default_factory=list)


# ── Public API ───────────────────────────────────────────────────────────────


def detect_fanout(
    *,
    summary: str | None,
    description: str | None,
    development_info: dict | None = None,
    testing_context: dict | None = None,
) -> FanoutContext | None:
    """Decide whether a ticket needs shared-component fan-out.

    Returns a populated :class:`FanoutContext` when the rule fires; ``None``
    when it doesn't (the ticket either scopes itself to a single role, or
    has no evidence of touching a shared component).
    """
    testing_context = testing_context or {}

    if _is_role_scoped(summary, description, testing_context):
        return None

    ac_texts = list(_ac_bullets(description, testing_context))
    text_blob = _text_blob(summary, description, ac_texts)

    ctx = FanoutContext()

    shared_paths = _shared_component_paths(development_info)
    if shared_paths:
        ctx.shared_paths = shared_paths
        ctx.triggers.append("pr_file_in_shared_component_dir")

    if any(p.search(text_blob) for p in _SHARED_LANGUAGE_PATTERNS):
        ctx.triggers.append("shared_language_in_ticket_text")

    detected_fields = _detect_fields(text_blob)
    if detected_fields:
        ctx.fields = detected_fields
        # A field with divergent consumption (only some KNOWN_ROLES) is by
        # itself strong evidence that the shared component fans out.
        if any(0 < len(f.consumed_by) < len(KNOWN_ROLES) for f in detected_fields):
            ctx.triggers.append("role_divergent_field_in_ac")

    if not ctx.triggers:
        return None

    return ctx


def render_fanout_guidance(ctx: FanoutContext) -> str:
    """Render the prompt block for a fired fan-out.

    Includes the SK-1898 → SK-2511 worked example verbatim, the detected
    fields with their per-role consumption, and the required output shape
    (per-role cases + per-role negative-space assertions).
    """
    lines: list[str] = []
    lines.append("")
    lines.append("━" * 69)
    lines.append("⚠️ CRITICAL: SHARED-COMPONENT FAN-OUT — PLAN PER CONSUMING ROLE")
    lines.append("━" * 69)
    lines.append("")
    lines.append(
        "This ticket touches a component that is consumed by more than one "
        "calculator role, but the ticket text does NOT scope the change to a "
        "single role. A plan that emits only positive-space checks against "
        "the shared component will ship the change to roles that shouldn't "
        "consume it — the bug will be reported months later after a real "
        "user hits it."
    )
    lines.append("")
    lines.append("**Worked example — DO NOT REPRODUCE THIS FAILURE:**")
    lines.append(
        "  SK-1898 asked us to \"display assessed value and loan amount in the "
        "Property Tax Results modal.\" The modal is a shared component consumed "
        "by both the buyer estimate and the seller net sheet. Loan amount is "
        "meaningful on the seller side (feeds mortgage-balance amortization) "
        "but meaningless on the buyer side (the buyer calculator explicitly "
        "does not consume it). The AC did not distinguish between roles, so "
        "any test plan derived from that ticket verified only \"field renders "
        "when data exists\" — and passed. Six months later the misplaced field "
        "was reported as bug SK-2511."
    )
    lines.append("")
    lines.append("**Detected signals for THIS ticket:**")
    if ctx.shared_paths:
        preview = ", ".join(ctx.shared_paths[:5])
        if len(ctx.shared_paths) > 5:
            preview += f" (+{len(ctx.shared_paths) - 5} more)"
        lines.append(f"  - PR touches shared-component path(s): {preview}")
    if "shared_language_in_ticket_text" in ctx.triggers:
        lines.append(
            "  - Ticket text uses shared-component language "
            "(\"shared\", \"modal\", \"results view\") without a role qualifier"
        )
    if ctx.fields:
        lines.append("  - Fields named in the ticket with known per-role consumption:")
        for f in ctx.fields:
            if f.is_mapped:
                consumers = ", ".join(sorted(f.consumed_by))
                lines.append(f"      • {f.name} → consumed by: {consumers}")
            else:
                lines.append(
                    f"      • {f.name} → consumption UNKNOWN "
                    f"(not in the fan-out map; treat diagnostically)"
                )
    lines.append("")

    role_list = ", ".join(f"`{r}`" for r in ctx.roles)
    lines.append(f"**REQUIRED OUTPUT SHAPE — do all of the following:**")
    lines.append("")
    lines.append(
        f"1. Emit AT LEAST ONE `happy_path` case PER consuming role "
        f"({role_list}). Do NOT collapse the roles into one generic case — "
        "the whole point is that the two roles diverge in what they should "
        "consume."
    )
    lines.append("")
    lines.append(
        "2. For each role, emit an explicit NEGATIVE-SPACE assertion in the "
        "role's case (or as a sibling case). The pattern is: \"Verify that "
        "<field(s)> DO NOT appear on <role>.\" The negative-space fields for "
        "a role are the ones the ticket introduces that the role does NOT "
        "consume, per the consumption map above."
    )
    lines.append("")
    if ctx.fields:
        lines.append("**Per-role negative-space directives for this ticket:**")
        for role in ctx.roles:
            not_consumed_mapped = [
                f.name for f in ctx.fields
                if f.is_mapped and role not in f.consumed_by
            ]
            unknown = [f.name for f in ctx.fields if not f.is_mapped]
            if not_consumed_mapped:
                joined = ", ".join(not_consumed_mapped)
                lines.append(
                    f"  - `{role}` case MUST assert: {joined} does NOT appear "
                    f"on the {role} surface. This is a hard assertion — the "
                    f"consumption map confirms the {role} calculator does not "
                    f"read these fields, so if they render on this surface the "
                    f"test must fail."
                )
            if unknown:
                joined = ", ".join(unknown)
                lines.append(
                    f"  - For `{role}`, {joined} has UNKNOWN role consumption. "
                    f"Emit a diagnostic step: \"Capture whether {joined} "
                    f"renders on the {role} surface and flag for PM — do NOT "
                    f"mark Pass until the intended consumers are confirmed.\" "
                    f"Do not assert absence or presence as a pass criterion "
                    f"until PM confirms which roles should consume this field."
                )
            if not not_consumed_mapped and not unknown:
                lines.append(
                    f"  - `{role}` case: all detected fields are consumed by "
                    f"this role — a standard positive-space check is sufficient."
                )
        lines.append("")

    lines.append(
        "3. Title each per-role case with an explicit role marker, e.g. "
        "`[Buyer estimate] Property Tax Results modal shows assessed value` "
        "and `[Seller net sheet] Property Tax Results modal shows loan "
        "amount and assessed value`. This makes the fan-out legible to QA "
        "without reading the JSON tags."
    )
    lines.append("")
    lines.append(
        "**Escape hatch (do NOT skip lightly):** if you are highly confident, "
        "from evidence IN THE TICKET or the diff, that this change genuinely "
        "applies to only one role, you may emit a single-role plan. In that "
        "case add a `grounding_warnings` entry with `missing_element` = "
        "\"shared-component fan-out suppressed\" and `explanation` naming the "
        "single-role evidence you relied on — so the reviewer can second-guess "
        "you if the evidence is thin."
    )
    lines.append("")
    return "\n".join(lines)


# ── Internal helpers ─────────────────────────────────────────────────────────


def _is_role_scoped(
    summary: str | None,
    description: str | None,
    testing_context: dict,
) -> bool:
    """True when the ticket clearly scopes itself to a single calculator
    role (buyer OR seller). If either is true, skip fan-out."""
    candidates: list[str] = []
    if summary:
        candidates.append(summary)
    if description:
        candidates.append(description)
    ac_from_ctx = testing_context.get("acceptanceCriteria")
    if isinstance(ac_from_ctx, str) and ac_from_ctx.strip():
        candidates.append(ac_from_ctx)

    for text in candidates:
        for pattern in _ROLE_SCOPING_PATTERNS:
            if pattern.search(text):
                return True
    return False


def _ac_bullets(description: str | None, testing_context: dict) -> Iterable[str]:
    """Yield AC bullets from both the description and the testingContext
    payload. The user-provided AC in testing_context is usually a raw string,
    not a bulleted list — we treat the whole blob as one AC entry."""
    yield from (extract_acceptance_criteria(description) or [])
    ac = testing_context.get("acceptanceCriteria")
    if isinstance(ac, str) and ac.strip():
        yield ac.strip()


def _text_blob(
    summary: str | None,
    description: str | None,
    ac_texts: Iterable[str],
) -> str:
    return "\n".join(
        [s for s in (summary, description) if s]
        + [a for a in ac_texts if a]
    )


def _shared_component_paths(development_info: dict | None) -> list[str]:
    """Return every PR-file path (from all linked PRs) that matches the
    shared-component allowlist. Dedupes and preserves discovery order."""
    if not development_info:
        return []
    seen: set[str] = set()
    hits: list[str] = []
    for pr in development_info.get("pull_requests") or []:
        for change in (pr or {}).get("files_changed") or []:
            filename = (change or {}).get("filename")
            if not isinstance(filename, str) or not filename:
                continue
            if filename in seen:
                continue
            if _path_is_shared(filename):
                seen.add(filename)
                hits.append(filename)
    return hits


def _path_is_shared(path: str) -> bool:
    for prefix in SHARED_COMPONENT_DIRS:
        if path.startswith(prefix):
            return True
    for pattern in SHARED_COMPONENT_FILE_PATTERNS:
        if pattern.search(path):
            return True
    return False


def _detect_fields(text_blob: str) -> list[FanoutField]:
    """Scan ``text_blob`` for known field names, case-insensitive, on
    word boundaries. Preserves the map's declared order for deterministic
    prompt output."""
    if not text_blob:
        return []
    lower = text_blob.lower()
    found: list[FanoutField] = []
    for field_name, consumers in ROLE_CONSUMPTION_MAP.items():
        pattern = re.compile(rf"\b{re.escape(field_name)}\b", re.IGNORECASE)
        if pattern.search(lower):
            found.append(FanoutField(name=field_name, consumed_by=consumers))
    return found
