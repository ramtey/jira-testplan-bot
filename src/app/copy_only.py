"""Copy-only ticket detection and the plan-shape rule it triggers.

Why this exists
---------------
The generator's default shape — one case per AC, per route, per state —
is right for a behavioural change and badly wrong for a copy change. On
a ticket whose whole diff is user-visible strings plus the constants
that select between them, that default produces a plan where most cases
re-open the same dialog to read a different sentence, one case per
retired string, a layout case per variant, and a tail of "does saving
still work" cases whose behaviour the PR's own component tests already
cover. QA runs thirty cases to verify six sentences.

The rule this module carries says: on a copy-only ticket the manual
checklist is

    (number of distinct user-visible string variants) + 4, maximum

and names exactly what the four are for (a retired-copy sweep, a
narrowest-width layout check against the longest variant, an error/empty
state, a keyboard/focus pass) and what is excluded (persistence,
mutation payloads, cache invalidation, prop wiring — all unit-test
territory).

Two halves, deliberately split
------------------------------
1. ``detect_copy_only`` is **deterministic** and reads only the diff. It
   never decides that a ticket IS copy-only — it decides whether there
   is enough evidence to be worth *asking*. It extracts the candidate
   string variants and the retired strings while it's there, because
   those are cheap to pull out of a patch and expensive for the model to
   re-derive.

2. ``render_copy_only_guidance`` emits a prompt block that hands the
   model the rule, the extracted evidence, and — first — the
   disqualifiers. The model does the classification, and the block says
   in as many words that a diff which changes what the code *does*, not
   just how it is called, is not copy-only and the block must be
   ignored.

That split is the whole safety argument. A false positive from the
detector costs prompt tokens; it cannot shrink a plan on its own,
because the shrinking only happens if the model agrees the diff is copy.
A deterministic detector that decided on its own would be the opposite
trade: silent under-testing on the tickets it misread.

The model reports its verdict back in the plan's ``copy_only`` field
(see ``SUBMIT_TEST_PLAN_TOOL``), which is what makes
``audit_plan_shape`` possible: "budget check before emitting" is only
a real check if something downstream can see whether it happened.

Scope: single-ticket plans only. A multi-ticket batch mixes tickets, and
a case budget derived from one ticket's copy variants has no meaning
over a batch that also contains a backend change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ── File classification ──────────────────────────────────────────────────────

# Files whose contents ARE copy. A changed line here counts as a copy change
# even when the line is a whole new key rather than an edit to an existing
# string — adding `"emptyState.title": "Nothing here yet"` to a locale bundle
# is exactly the copy change this rule is about.
COPY_FILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)(locales?|i18n|lang|translations?)/", re.IGNORECASE),
    re.compile(r"(^|/)(strings|copy|messages|labels)\.(ts|tsx|js|jsx|json|ya?ml)$", re.IGNORECASE),
    re.compile(r"\.strings\.(ts|tsx|js|jsx|json)$", re.IGNORECASE),
    re.compile(r"(^|/)[a-z]{2}(-[A-Za-z]{2,4})?\.json$"),  # en.json, en-US.json, pt-BR.json
)

# Tests are explicitly inside the rule's definition of copy-only ("plus their
# tests"), so they neither prove nor disprove the classification. They are
# dropped before the verdict rather than counted as behavioural change.
TEST_FILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\.(test|spec)\.[a-z]+$", re.IGNORECASE),
    re.compile(r"(^|/)(__tests__|__snapshots__|tests?)/", re.IGNORECASE),
    re.compile(r"\.snap$", re.IGNORECASE),
    re.compile(r"(^|/)test_[^/]+\.py$"),
)

# Neither evidence for nor against. Lockfiles and generated bundles carry no
# behaviour; docs and changelogs are copy, but not the *user-visible product
# copy* this rule sizes a plan against — a README edit is a documentation
# ticket, which the deliverable classifier already routes elsewhere.
IGNORABLE_FILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.ya?ml|uv\.lock|Podfile\.lock|Cargo\.lock)$"),
    re.compile(r"\.(md|mdx|txt|rst)$", re.IGNORECASE),
    re.compile(r"(^|/)CHANGELOG", re.IGNORECASE),
)


# ── String-literal handling ──────────────────────────────────────────────────

# Single, double and back-quoted literals, honouring backslash escapes. Good
# enough for diff lines, which is all this ever sees: a literal that spans
# lines shows up as two unmatched fragments and simply fails to normalize,
# which pushes the file into the conservative "behavioural" bucket.
_STRING_LITERAL_RE = re.compile(
    r'"(?:[^"\\\n]|\\.)*"'
    r"|'(?:[^'\\\n]|\\.)*'"
    r"|`(?:[^`\\]|\\.)*`"
)

_LITERAL_PLACEHOLDER = '"\x00"'

# A quoted literal that is plainly not user-visible copy: an identifier, an
# import path, a testID, a CSS class, a hex colour, a bare number.
_NOT_COPY_LITERAL_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^[a-z0-9_$.\-/@]*$"),          # lowercase identifier / path / key
    # A dotted / dashed / slashed identifier of any casing: an i18n key
    # (`leaveGroup.title`), an import path, a CSS class. Requires a separator,
    # so a one-word button label like 'Cancel' is still treated as copy.
    re.compile(r"^[A-Za-z0-9_$]+(?:[.\-/][A-Za-z0-9_$]+)+$"),
    re.compile(r"^#[0-9a-fA-F]{3,8}$"),         # hex colour
    re.compile(r"^[\d.,%$\s+\-]*$"),            # numbers and punctuation only
    re.compile(r"^[A-Z0-9_]+$"),                # SCREAMING_CASE constant
    re.compile(r"^\S*/\S*$"),                   # a path with no spaces
)

_MIN_COPY_LITERAL_CHARS = 3

# How much of a diff has to be copy before a ticket that *says* it is copy-only
# is taken at its word. Set below 1.0 deliberately: the rule counts an
# accompanying refactor (a hook signature moving to an options object, a
# service returning context it used to short-circuit) as copy-only, and those
# refactors are real non-string lines. Below this, the declared phrase is
# treated as stale scope talk rather than as a description of this diff.
DECLARED_COPY_LINE_RATIO = 0.5

# Enough evidence for the model to work with; past this the block is just
# prompt weight. Truncation is reported so the model knows the list is partial.
MAX_REPORTED_STRINGS = 40

# Phrases in a summary, description, PR body or commit that assert the change
# is copy. Only ever combined with diff evidence — on its own a phrase is a
# claim about intent, and intent drifts from diffs.
_SCOPE_PHRASE_RE = re.compile(
    r"copy[\s-]?only"
    r"|copy change"
    r"|microcopy"
    r"|re-?word"
    r"|wording"
    r"|text(?:ual)? change"
    r"|string (?:update|change)"
    r"|update(?:s|d)? (?:the )?(?:copy|wording|label|text|string)"
    r"|(?:label|title|heading) (?:copy )?change"
    r"|re-?label"
    r"|typo",
    re.IGNORECASE,
)


@dataclass
class CopyOnlyContext:
    """Diff-derived evidence that a ticket may be copy-only.

    Deliberately not a verdict — see the module docstring. ``signal``
    records WHY the block is being shown so a prompt log explains itself
    months later:

    * ``strings_only`` — every changed non-test file was either a copy
      file or a file whose changed lines differ only inside string
      literals. The strong case.
    * ``declared`` — the ticket or its PR says the change is copy, and at
      least ``DECLARED_COPY_LINE_RATIO`` of the changed non-test lines
      are copy. The refactor-alongside-copy case.
    """

    signal: str
    added_strings: list[str] = field(default_factory=list)
    removed_strings: list[str] = field(default_factory=list)
    copy_files: list[str] = field(default_factory=list)
    other_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    scope_phrases: list[str] = field(default_factory=list)
    strings_truncated: bool = False

    @property
    def candidate_variant_count(self) -> int:
        """How many distinct added strings look like user-visible copy.

        A *candidate* count: two of these strings can be the two halves
        of one dialog, and one of them can branch into three variants the
        diff never spells out. The model reconciles that; this number
        exists so the block can say "roughly this many" instead of
        nothing.
        """
        return len(self.added_strings)


def _matches_any(path: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(p.search(path) for p in patterns)


def is_test_file(path: str) -> bool:
    return _matches_any(path, TEST_FILE_PATTERNS)


def is_copy_file(path: str) -> bool:
    return _matches_any(path, COPY_FILE_PATTERNS)


def is_ignorable_file(path: str) -> bool:
    return _matches_any(path, IGNORABLE_FILE_PATTERNS)


def _normalize_line(line: str) -> str:
    """A changed line with every string literal's *contents* blanked out.

    Two lines that normalize to the same thing differ only inside their
    quotes — which is the mechanical definition of a copy change.
    """
    return " ".join(_STRING_LITERAL_RE.sub(_LITERAL_PLACEHOLDER, line).split())


def _changed_lines(patch: str) -> tuple[list[str], list[str]]:
    """``(added, removed)`` content lines from a unified diff patch.

    File headers (``+++`` / ``---``) are not content and are dropped; so
    are lines that are empty once the marker is stripped, because a
    whitespace-only line pair would otherwise read as a behavioural
    change on a file whose real change was a reflow.
    """
    added: list[str] = []
    removed: list[str] = []
    for raw in (patch or "").splitlines():
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            body = raw[1:]
            if body.strip():
                added.append(body)
        elif raw.startswith("-"):
            body = raw[1:]
            if body.strip():
                removed.append(body)
    return added, removed


def is_copy_literal(literal: str) -> bool:
    """Whether a quoted literal reads as user-visible product copy."""
    inner = literal[1:-1] if len(literal) >= 2 else literal
    inner = inner.strip()
    if len(inner) < _MIN_COPY_LITERAL_CHARS:
        return False
    return not any(p.match(inner) for p in _NOT_COPY_LITERAL_RES)


def extract_copy_literals(lines: list[str]) -> list[str]:
    """Distinct user-visible-looking literals in ``lines``, order preserved."""
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        for literal in _STRING_LITERAL_RE.findall(line):
            if not is_copy_literal(literal):
                continue
            inner = literal[1:-1].strip()
            if inner in seen:
                continue
            seen.add(inner)
            out.append(inner)
    return out


def _iter_changed_files(development_info: dict | None):
    """Every ``files_changed`` entry across the ticket's pull requests.

    Closed-unmerged PRs are already gone by the time this runs —
    ``source_grounding.filter_development_info`` drops them before
    anything reads ``development_info`` — so no state filtering is
    repeated here.
    """
    for pr in ((development_info or {}).get("pull_requests") or []):
        if not isinstance(pr, dict):
            continue
        for fc in (pr.get("files_changed") or []):
            if isinstance(fc, dict) and fc.get("filename"):
                yield fc


def _scope_phrases(
    summary: str | None,
    description: str | None,
    development_info: dict | None,
) -> list[str]:
    """Distinct scope-limiting phrases found in the ticket and its PRs."""
    haystacks: list[str] = [summary or "", description or ""]
    for pr in ((development_info or {}).get("pull_requests") or []):
        if isinstance(pr, dict):
            haystacks.append(pr.get("title") or "")
            haystacks.append(pr.get("github_description") or "")
    for commit in ((development_info or {}).get("commits") or []):
        if isinstance(commit, dict):
            haystacks.append(commit.get("message") or "")

    found: list[str] = []
    seen: set[str] = set()
    for text in haystacks:
        for match in _SCOPE_PHRASE_RE.findall(text):
            phrase = match.lower().strip()
            if phrase and phrase not in seen:
                seen.add(phrase)
                found.append(phrase)
    return found


def detect_copy_only(
    summary: str | None,
    description: str | None,
    development_info: dict | None,
) -> CopyOnlyContext | None:
    """Evidence that this ticket's diff is limited to user-visible copy.

    Returns ``None`` — meaning "don't show the block" — whenever the
    evidence is absent OR unreadable. Unreadable matters: a PR whose
    patches GitHub did not return tells us nothing about what changed,
    and a plan shrunk on the strength of a diff nobody read is the worst
    outcome this module could produce. No patches, no block.
    """
    copy_files: list[str] = []
    other_files: list[str] = []
    test_files: list[str] = []
    added_copy_lines: list[str] = []
    removed_copy_lines: list[str] = []
    copy_line_count = 0
    other_line_count = 0
    saw_patch = False

    for fc in _iter_changed_files(development_info):
        path = fc["filename"]
        if is_ignorable_file(path):
            continue
        if is_test_file(path):
            test_files.append(path)
            continue

        patch = fc.get("patch")
        if not patch:
            # A behavioural file we cannot read. Treat it as behavioural —
            # the conservative direction — and let the ratio/strings-only
            # checks below fail on it.
            other_files.append(path)
            other_line_count += int(fc.get("changes") or 0) or 1
            continue

        saw_patch = True
        added, removed = _changed_lines(patch)
        line_total = len(added) + len(removed)
        if not line_total:
            continue

        norm_added = sorted(_normalize_line(line) for line in added)
        norm_removed = sorted(_normalize_line(line) for line in removed)
        string_edit_only = bool(added or removed) and norm_added == norm_removed

        if is_copy_file(path) or string_edit_only:
            copy_files.append(path)
            copy_line_count += line_total
            added_copy_lines.extend(added)
            removed_copy_lines.extend(removed)
        else:
            other_files.append(path)
            other_line_count += line_total

    if not saw_patch or not copy_files:
        return None

    scope_phrases = _scope_phrases(summary, description, development_info)

    if not other_files:
        signal = "strings_only"
    else:
        total = copy_line_count + other_line_count
        ratio = copy_line_count / total if total else 0.0
        if scope_phrases and ratio >= DECLARED_COPY_LINE_RATIO:
            signal = "declared"
        else:
            return None

    added_strings = extract_copy_literals(added_copy_lines)
    removed_all = extract_copy_literals(removed_copy_lines)
    # A string present on both sides was reindented or moved, not retired.
    added_set = set(added_strings)
    removed_strings = [s for s in removed_all if s not in added_set]

    truncated = (
        len(added_strings) > MAX_REPORTED_STRINGS
        or len(removed_strings) > MAX_REPORTED_STRINGS
    )

    return CopyOnlyContext(
        signal=signal,
        added_strings=added_strings[:MAX_REPORTED_STRINGS],
        removed_strings=removed_strings[:MAX_REPORTED_STRINGS],
        copy_files=copy_files,
        other_files=other_files,
        test_files=test_files,
        scope_phrases=scope_phrases,
        strings_truncated=truncated,
    )


def case_budget(variant_count: int) -> int:
    """Manual-case ceiling for a copy-only plan: variants + 4.

    The four are the retired-copy sweep, the narrowest-width layout
    check, the error/empty state, and the keyboard/focus pass. A plan
    with fewer variants than that does not get to spend the difference
    elsewhere — the four are a ceiling on the non-variant cases, not a
    quota to fill.
    """
    return max(int(variant_count or 0), 0) + 4


# ── Prompt block ─────────────────────────────────────────────────────────────

# Leads with the disqualifiers, on purpose. The detector's job was recall; if
# the block opened with the budget, a diff that merely *looks* like copy would
# get a copy-sized plan before the model ever considered whether behaviour
# changed. Everything after the classification gate is conditional on passing
# it.
_COPY_ONLY_RULE = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ COPY-ONLY TICKET RULE — CLASSIFY THE DIFF FIRST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This ticket's diff looks like it may be COPY-ONLY. Decide whether it is
before you use any of the shape rules below, and report your decision in the
`copy_only` field of `submit_test_plan` either way.

**A ticket is COPY-ONLY when the behavioral change is limited to user-visible
strings and the constants/helpers that select between them — plus their
tests.** A refactor that accompanies a copy change (a hook signature moving
from positional args to an options object, a service returning context it
previously short-circuited) is still COPY-ONLY — unless the diff changes what
the code DOES, not just how it is called.

**It is NOT copy-only if** the diff adds or removes a branch a user can reach,
changes a value that is computed rather than displayed, changes what is
persisted or requested, adds or removes a control, or changes when something
renders. If you are unsure, it is NOT copy-only: set
`copy_only.verdict = "not_copy_only"`, say why in `copy_only.rationale`, IGNORE
every rule below, and plan this ticket normally.

**When it IS copy-only, generate the plan to this shape and no larger:**

1. ONE case per distinct user-visible string variant, not per route to it.
   If copy branches on scope/role/state, each branch is one case: assert the
   exact title, body and button labels, then cancel. Do not split "open the
   dialog" and "read the dialog" into separate cases.

2. ONE retired-copy sweep case for the whole screen. List every removed
   string in that single case. Never one case per removed string.

3. ONE layout case, run at the narrowest supported width, against the
   LONGEST variant only. Shorter variants cannot clip if the longest does not.

4. ONE case per error/empty state that renders its own copy.

5. ONE keyboard/focus case if the changed control is interactive.

**Then apply these exclusions — these are NOT manual cases in a copy-only plan:**

- Persistence ("does saving still work"), mutation payload shape, cache
  invalidation, and prop wiring. If unit tests cover them, set
  `covered_by_unit_test: true` with a `unit_test_ref` and leave them out of the
  manual checklist. Check the PR's test files before asserting they are
  uncovered — a copy PR that adds 200+ lines of component tests almost
  certainly covers its own wiring.
- Any case whose expected result you could not read from the diff. Those
  belong in NEEDS SPEC: set `expected_verified: false` and phrase `expected`
  as the open question. Do not pad the checklist with unverifiable behavior.
- Regression items for code the diff touched but did not behaviorally change.
  Collapse those into at most ONE `regression_checklist` line naming the
  refactored seam.

**Merge aggressively:**
- All dismissal routes (Cancel, Escape, outside click) = ONE case.
- All variants of "control is disabled while X is pending" = ONE case.

**BUDGET CHECK BEFORE EMITTING.** Count the manual cases you are about to
submit — every entry in `happy_path`, `edge_cases` and `integration_tests` that
is NOT marked `covered_by_unit_test`. The target is:

    (number of distinct copy variants) + 4, MAXIMUM

If you exceed it, cut from the exclusion list above — never from rule 1. Put
the count and the variant count in `copy_only` so the budget check is on the
record.

**FIXTURE HONESTY.** For each scope-dependent variant, state in that case's
`preconditions` the account capability it requires (e.g. "manages 2+ groups",
"brokerage-level permission"). If one account cannot reach every variant, list
those variants in `copy_only.unreachable_variants` and say so in
`how_to_see_it.reason` — QA must learn that at the top of the plan, not by
discovering halfway down that a case is unreachable.

**DESTRUCTIVE MARKING.** Any case whose verification requires writing to or
clearing shared environment data must start its `title` with "DESTRUCTIVE — "
and name the blast radius in `preconditions`: whose data changes, and who else
sees it.
"""


def render_copy_only_guidance(ctx: CopyOnlyContext) -> str:
    """The copy-only prompt block, with this ticket's diff evidence attached.

    The evidence is what stops the model re-deriving the variant list from
    prose and getting a different answer than the audit will. It is
    labelled as *candidates* throughout: the extractor cannot tell a
    dialog title from its body, and it cannot see a variant the diff
    expresses as a branch rather than as a literal.
    """
    out = [_COPY_ONLY_RULE]
    out.append("\n**WHAT THE DIFF ACTUALLY SHOWS** (extracted mechanically — "
               "treat as candidates, not as the answer):\n")

    if ctx.signal == "strings_only":
        out.append(
            "- Every changed non-test file is either a copy/i18n file or a file "
            "whose changed lines differ ONLY inside string literals. No "
            "behavioural line changed in any file this pass could read.\n"
        )
    else:
        out.append(
            "- The ticket or its PR describes the change as copy "
            f"({', '.join(ctx.scope_phrases[:4])}), and most changed lines are "
            "string content — but these files also changed outside their string "
            f"literals: {', '.join(ctx.other_files[:8])}. Read those diffs before "
            "you classify: if any of them changes behaviour, this is NOT a "
            "copy-only ticket.\n"
        )

    if ctx.copy_files:
        out.append(f"- Copy/string files changed: {', '.join(ctx.copy_files[:12])}\n")
    if ctx.test_files:
        out.append(
            f"- Test files changed ({len(ctx.test_files)}): "
            f"{', '.join(ctx.test_files[:8])}. Read them BEFORE writing any "
            "persistence / wiring / payload case — that is what the exclusion "
            "list means by 'check the PR's test files'.\n"
        )

    if ctx.added_strings:
        out.append(
            f"\n**Candidate new/changed strings ({len(ctx.added_strings)}"
            f"{'+, truncated' if ctx.strings_truncated else ''}):**\n"
        )
        for s in ctx.added_strings:
            out.append(f"  - {s!r}\n")
        out.append(
            "\nThese are literals, not variants. Several of them can belong to "
            "ONE dialog (title + body + button labels = one case, per rule 1), "
            "and one of them can render as several variants the diff selects "
            "between. Count VARIANTS, then set `copy_only.variant_count`.\n"
        )

    if ctx.removed_strings:
        out.append(
            f"\n**Retired strings ({len(ctx.removed_strings)}"
            f"{'+, truncated' if ctx.strings_truncated else ''}) — these go in "
            "the ONE sweep case of rule 2, never one case each:**\n"
        )
        for s in ctx.removed_strings:
            out.append(f"  - {s!r}\n")

    if ctx.added_strings:
        out.append(
            f"\n**Ceiling, not a target.** {ctx.candidate_variant_count} candidate "
            "strings were found. If — and only if — every one of them turned out "
            "to be its own variant, the budget would be "
            f"{case_budget(ctx.candidate_variant_count)} manual cases. That is "
            "almost never how it lands: a dialog's title, body and button labels "
            "are ONE variant. Count YOUR variants first, then compute the budget "
            "from that count, not from this one.\n"
        )

    return "".join(out)


# ── Post-generation audit ────────────────────────────────────────────────────


def _manual_cases(test_plan) -> list[dict]:
    """Cases that land on QA's manual checklist.

    ``covered_by_unit_test`` cases are excluded because the UI files them
    into a separate "already covered" list QA skips — counting them would
    penalise the model for doing exactly what the exclusion list asks.
    ``regression_checklist`` is excluded because it is a list of strings,
    not cases, and rule 3's collapse already bounds it.

    ``security_negative_tests`` is excluded for a different reason: those
    cases are additive by construction (see src/app/security_surfaces.py)
    and are triggered by the diff touching a router, an upload handler or a
    vendor call — none of which a genuinely copy-only diff does. Counting
    them here would report an overrun against a budget derived from copy
    variants, and the reviewer would be told to cut security coverage to
    make room for string variants.
    """
    out: list[dict] = []
    for section in ("happy_path", "edge_cases", "integration_tests"):
        for case in (getattr(test_plan, section, None) or []):
            if isinstance(case, dict) and not case.get("covered_by_unit_test"):
                out.append(case)
    return out


def audit_plan_shape(test_plan) -> dict | None:
    """Check the emitted plan against the budget the model claimed to apply.

    Returns ``None`` when the model did not classify the ticket as
    copy-only — there is no budget to audit, and a plan that was never
    meant to be copy-shaped must not be reported as over one.

    This never edits the plan. Deciding WHICH case to cut is a judgement
    the rule assigns to the model ("cut from the exclusion list, never
    from rule 1"), and a mechanical pass that dropped the last N cases
    would cut whichever ones happened to sort last — including a variant
    case, which is the one thing the rule forbids. What it does instead
    is make the overrun visible: to the log, to the provenance panel a
    tester reads, and to the replay eval, which can now measure whether
    the rule is holding instead of anyone guessing.
    """
    declared = getattr(test_plan, "copy_only", None)
    if not isinstance(declared, dict):
        return None
    if (declared.get("verdict") or "").strip().lower() != "copy_only":
        return None

    manual = _manual_cases(test_plan)
    raw_variants = declared.get("variant_count")
    variant_count = raw_variants if isinstance(raw_variants, int) and raw_variants >= 0 else None
    budget = case_budget(variant_count) if variant_count is not None else None

    result = {
        "verdict": "copy_only",
        "variant_count": variant_count,
        "budget": budget,
        "manual_cases": len(manual),
        "within_budget": None if budget is None else len(manual) <= budget,
        "rationale": declared.get("rationale") or "",
    }
    unreachable = declared.get("unreachable_variants")
    if isinstance(unreachable, list) and unreachable:
        result["unreachable_variants"] = [str(u) for u in unreachable if str(u).strip()]
    return result
