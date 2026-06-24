"""
Analyze Jira issue descriptions for completeness — flag concrete gaps a QA tester
would have to chase the reporter/PM about (missing AC, missing repro steps, etc.).

Per-issue-type rules:
  - Bug: needs reproduction steps + expected-vs-actual behavior
  - everything else (Story/Task/Sub-task/Improvement/…): needs acceptance criteria

When the description is missing entirely, only the "Missing description" gap is
reported — the type-specific gaps would be noise.
"""

import re

from .models import DescriptionAnalysis

_AC_KEYWORDS = (
    "acceptance criteria",
    "ac:",
    "acceptance:",
    "should:",
    "must:",
    "given ",
    " when ",
    " then ",
)

_REPRO_KEYWORDS = (
    "steps to reproduce",
    "reproduction steps",
    "to reproduce",
    "repro:",
    "reproduce:",
    "repro steps",
)

_EXPECTED_ACTUAL_KEYWORDS = (
    "expected result",
    "expected behavior",
    "expected behaviour",
    "expected:",
    "actual result",
    "actual behavior",
    "actual behaviour",
    "actual:",
)


def analyze_description(
    description: str | None,
    issue_type: str | None = None,
) -> DescriptionAnalysis:
    """Detect concrete gaps in a Jira description for a QA reader."""
    clean_text = (description or "").strip()
    char_count = len(clean_text)
    word_count = len(clean_text.split()) if clean_text else 0

    if not clean_text:
        return DescriptionAnalysis(
            has_description=False,
            gaps=["Missing description"],
            char_count=0,
            word_count=0,
        )

    lower_text = clean_text.lower()
    gaps: list[str] = []
    issue_type_lower = (issue_type or "").lower()

    if issue_type_lower == "bug":
        if not any(k in lower_text for k in _REPRO_KEYWORDS):
            gaps.append("Missing reproduction steps")
        if not any(k in lower_text for k in _EXPECTED_ACTUAL_KEYWORDS):
            gaps.append("Missing expected vs. actual behavior")
    else:
        if not any(k in lower_text for k in _AC_KEYWORDS):
            gaps.append("Missing acceptance criteria")

    return DescriptionAnalysis(
        has_description=True,
        gaps=gaps,
        char_count=char_count,
        word_count=word_count,
    )


# Heading variants we treat as the start of an Acceptance Criteria block.
# Match must be the whole non-bullet content on a line (after stripping markdown
# heading markers and bold wrappers), case-insensitive.
_AC_HEADING_RE = re.compile(
    r"""^                       # start
        (?:\#{1,6}\s+)?         # optional markdown heading hashes
        \**\s*                  # optional bold markers
        (?:acceptance\s+criteria|ac)   # heading text
        \s*\**                  # trailing bold markers
        \s*:?\s*                # optional trailing colon
        $""",
    re.IGNORECASE | re.VERBOSE,
)

# Headings that begin a *different* section — once we hit one, the AC block is over.
# We intentionally end at any new header line; AC blocks are usually short.
_NEXT_SECTION_RE = re.compile(r"^(?:\#{1,6}\s+|\*\*[^*]+\*\*\s*:?\s*$)")

# Stale-AC headings we explicitly skip per existing SYSTEM_PROMPT rule.
_STALE_AC_RE = re.compile(
    r"\b(?:og|old|original|previous|prior)\s+(?:ac|acceptance\s+criteria)\b",
    re.IGNORECASE,
)

# Well-known section names that ALWAYS terminate the AC block, even when they
# are immediately followed by their own bullet list. Without this denylist the
# "grouping sub-label" rule below (a heading followed by bullets keeps the AC
# block open) would wrongly swallow an "Out of Scope" or "Implementation Notes"
# bullet list as acceptance criteria.
_TERMINAL_AC_SECTION_RE = re.compile(
    r"""^(?:
        implementation\s+notes? | technical\s+notes? | dev(?:eloper)?\s+notes? |
        notes? | out\s+of\s+scope | in\s+scope | scope |
        design(?:\s+notes?)? | designs? | mock-?ups? | wireframes? |
        qa(?:\s+notes?)? | test(?:ing)?\s+notes? | test\s+plan |
        open\s+questions? | questions? | assumptions? | risks? |
        references? | resources? | links? | attachments? |
        background | context | overview | summary | description |
        tasks? | sub-?tasks? | dependencies | definition\s+of\s+done | dod
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

# Bullet line with the text on the same line: "* Banner appears..." (markdown).
# Requires at least one non-whitespace character after the marker so this
# doesn't swallow bare-bullet lines (which are handled separately).
_BULLET_RE = re.compile(r"^\s*(?:[-*+•·]|\d+[.)])\s+(\S.*)$")

# Bullet marker alone on its own line: "• " or "- " with no following text.
# This is what Jira's ADF → plain-text conversion produces: each ``listItem``
# emits its bullet on one line and the inner paragraph text on the next.
_BARE_BULLET_RE = re.compile(r"^\s*(?:[-*+•·]|\d+[.)])\s*$")


_URL_LINE_RE = re.compile(r"^https?://\S+$")


def _looks_like_section_heading(stripped: str) -> bool:
    """Heuristic: short non-bullet line that looks like the start of a new section.

    Jira's ADF parser strips markdown ``##`` markers, so by the time the
    description reaches this code, "Implementation Notes" is indistinguishable
    from any other paragraph. We use a soft heuristic: a line is treated as a
    section heading when it's short, has no terminal punctuation, and is not
    a URL — which catches "Implementation Notes", "Notes", "Out of Scope", etc.
    without false-positives on AC bullets.
    """
    if not stripped:
        return False
    if _URL_LINE_RE.match(stripped):
        return False
    if len(stripped) > 60:
        return False
    if stripped.endswith((".", "!", "?", ",", ";")):
        return False
    # Markdown/bold headings — exact patterns
    if re.match(r"^\#{1,6}\s+", stripped):
        return True
    if re.match(r"^\*\*[^*]+\*\*\s*:?\s*$", stripped):
        return True
    # Short prose-like line ending with colon (e.g. "Notes:") — likely a heading
    if stripped.endswith(":") and len(stripped.split()) <= 6:
        return True
    # Short title-case-ish line of 1-5 words, no trailing punctuation — likely a heading
    words = stripped.split()
    if 1 <= len(words) <= 5 and stripped[0].isupper():
        # Avoid false positives on short single bullets like "Login works"
        # by requiring at least 2 words OR the line being followed by content.
        # Simplest test: most "heading" lines lack a verb. We approximate by
        # checking that the first word starts uppercase and the line doesn't
        # start with common AC verbs.
        first = words[0].lower()
        if first not in {
            "the", "a", "an", "when", "if", "user", "users",
            "verify", "ensure", "show", "shows", "display",
            "click", "clicking", "add", "adds", "edit", "save",
            "do", "don't", "should", "must", "all", "every",
        }:
            return True
    return False


# ─── compound / multi-verb AC facet extraction ───────────────────────────────
#
# A single AC bullet often enumerates several DISTINCT actions that each need
# their own test case, e.g.:
#   "A calculation being created, updated, or deleted is captured"
#       → created / updated / deleted   (three distinct mutations)
#   "A calculation being shared or sent (email/PDF) is captured"
#       → shared / sent / email / PDF   (two actions × two output channels)
#
# When the generator collapses these into a subset of cases (only created+
# updated, only the email half), coverage looks complete at the bullet level
# while a real behaviour ships untested. ``extract_ac_action_facets`` pulls the
# discrete facets out of one AC so coverage can be checked PER ACTION, not just
# per bullet. It is deliberately conservative: it fires only on clear action
# enumerations and slash-separated parentheticals, and returns ``[]`` (meaning
# "treat as a single behaviour") when in doubt. A clarifying field list such as
# "(name, email, phone, state/county)" is NOT split — those are attributes of
# one action, not separate actions.

# An action-like token: a gerund (-ing) or past participle (-ed), long enough
# to not be a stopword like "and"/"red". Captured so we can pull the literal.
_ACTION_WORD_RE = re.compile(r"^[a-z]{2,}(?:ing|ed)$")

# A few high-frequency irregular action words that don't end in -ing/-ed but
# routinely appear in enumerations alongside regular ones ("shared or sent").
_IRREGULAR_ACTION_WORDS = {"sent", "sold", "set", "built", "kept", "drawn", "shown"}

# Auxiliary / linking words that match the -ing/-ed shape but are NOT actions.
# Without this filter "a calculation being created" would yield two "facets"
# (being, created) and be mis-flagged as a compound AC.
_ACTION_STOPWORDS = {
    "being", "having", "doing", "used", "based", "related", "regarding",
    "concerning", "including", "involving", "resulting", "containing",
    "existing", "remaining", "corresponding", "following", "preceding",
    "given", "added",  # "added going forward" — not an enumerated action here
}


def _is_action_word(token: str) -> bool:
    t = token.strip().strip(".,;:()").lower()
    if t in _ACTION_STOPWORDS:
        return False
    return bool(_ACTION_WORD_RE.match(t)) or t in _IRREGULAR_ACTION_WORDS


def _parenthetical_variants(text: str) -> list[str]:
    """Pull slash-separated variants out of a parenthetical, e.g.
    "(email/PDF)" → ["email", "PDF"]. A comma-bearing parenthetical is a field
    clarification ("(name, email, phone, state/county)"), not an enumeration of
    distinct actions, so it is ignored.
    """
    out: list[str] = []
    for inner in re.findall(r"\(([^)]*)\)", text):
        if "," in inner:
            continue
        if "/" not in inner:
            continue
        parts = [p.strip() for p in inner.split("/") if p.strip()]
        # Require every part to be a short single word — avoids splitting
        # phrases like "(per user / per account basis)".
        if len(parts) >= 2 and all(len(p.split()) == 1 and len(p) <= 12 for p in parts):
            out.extend(parts)
    return out


def extract_ac_action_facets(ac_text: str) -> list[str]:
    """Return the distinct action facets enumerated in one AC bullet.

    Returns ``[]`` when the AC describes a single behaviour (the common case).
    A non-empty result means the AC enumerates multiple actions that each need
    their own test case / assertion. Order-preserving and de-duplicated; the
    original surface form of each facet is kept (for display in the prompt).
    """
    if not ac_text:
        return []

    facets: list[str] = []
    seen: set[str] = set()

    def _add(token: str) -> None:
        cleaned = token.strip().strip(".,;:()").strip()
        if not cleaned:
            return
        key = cleaned.lower()
        if key in seen:
            return
        seen.add(key)
        facets.append(cleaned)

    # 1) Action-word enumeration: a run of 2+ action words joined by
    #    ","/"or"/"and". We anchor on confirmed action words and pull in
    #    immediate connector-siblings so irregulars ("sent") come along.
    tokens = ac_text.split()
    run: list[str] = []

    def _flush_run() -> None:
        # A real enumeration needs at least two action words.
        action_members = [t for t in run if _is_action_word(t)]
        if len(action_members) >= 2:
            for t in action_members:
                _add(t)
    for raw_tok in tokens:
        tok = raw_tok.strip()
        bare = tok.strip(".,;:()").lower()
        if _is_action_word(tok):
            run.append(tok)
        elif bare in {"or", "and", ""} or tok.endswith(","):
            # Connector — keep the run open. A token like "sent," ends in a
            # comma but is also an action word; handled above first.
            continue
        else:
            _flush_run()
            run = []
    _flush_run()

    # 2) Slash-separated parenthetical variants: "(email/PDF)" → email, PDF.
    for variant in _parenthetical_variants(ac_text):
        _add(variant)

    # Only treat the AC as compound when ≥2 distinct facets were found.
    return facets if len(facets) >= 2 else []


def _next_nonblank_is_bullet(lines: list[str], start: int) -> bool:
    """True if the first non-blank line at/after ``start`` is a bullet.

    Used to decide whether a non-bullet line inside the AC block is a grouping
    sub-label (e.g. "Agent mobile app") that organizes the AC bullets into
    categories — those are followed by more bullets and must NOT terminate the
    block — versus the start of a genuinely different section.
    """
    j = start
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j >= len(lines):
        return False
    line = lines[j]
    return bool(_BULLET_RE.match(line) or _BARE_BULLET_RE.match(line.strip()))


def extract_acceptance_criteria(description: str | None) -> list[str]:
    """Pull the AC bullets out of a Jira description.

    Looks for the first ``Acceptance Criteria`` / ``AC`` heading and returns
    each bulleted or numbered item beneath it. Stops at the next section
    heading (markdown ``##``, bolded line, or short prose-like header — see
    ``_looks_like_section_heading``). Free-form paragraphs are ignored: real
    Jira ACs are virtually always bullets, and "no ACs found" is a far safer
    failure mode than capturing Figma URLs and "Implementation Notes" as ACs.

    Headings under ``OG AC`` / ``Old AC`` / ``Previous AC`` / etc. are skipped
    to match the existing SYSTEM_PROMPT rule about superseded requirements.
    """
    if not description:
        return []

    lines = description.splitlines()
    i = 0
    in_ac_block = False
    bullets: list[str] = []

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        # Strip markdown bold + heading markers, then check against AC heading.
        bare = re.sub(r"^\#{1,6}\s+", "", stripped)
        bare = re.sub(r"^\*\*\s*", "", bare)
        bare = re.sub(r"\s*\*\*\s*:?\s*$", "", bare).strip().rstrip(":").strip()

        if not in_ac_block:
            # Skip stale AC sections entirely — advance until we either hit
            # a clear section heading or the current AC heading.
            if _STALE_AC_RE.search(stripped):
                i += 1
                while i < len(lines):
                    nxt = lines[i].strip()
                    nxt_bare = re.sub(r"^\#{1,6}\s+", "", nxt)
                    nxt_bare = re.sub(r"^\*\*\s*", "", nxt_bare)
                    nxt_bare = re.sub(r"\s*\*\*\s*:?\s*$", "", nxt_bare).strip().rstrip(":").strip()
                    if _AC_HEADING_RE.match(nxt_bare) and not _STALE_AC_RE.search(nxt):
                        break
                    if _looks_like_section_heading(nxt) and not _STALE_AC_RE.search(nxt):
                        # A real section start outside an AC — stop skipping here
                        # so the outer loop can re-evaluate it.
                        break
                    i += 1
                continue
            if _AC_HEADING_RE.match(bare):
                in_ac_block = True
                i += 1
                continue
            i += 1
            continue

        # Inside AC block.
        if _STALE_AC_RE.search(stripped):
            break

        # Same-line bullet: "* Banner appears on buyer files".
        bullet_match = _BULLET_RE.match(raw)
        if bullet_match:
            text = bullet_match.group(1).strip()
            if text:
                bullets.append(text)
            i += 1
            continue

        # Bare bullet marker on its own line: "• " followed by the text on
        # the next non-empty line. This is the shape Jira's ADF → plain text
        # converter produces (one listItem = one "• " line + paragraph below).
        # Bullet content wins over the section-heading heuristic: a short
        # title-case bullet like "Banner appears..." would otherwise be
        # misclassified and abort the AC block.
        if _BARE_BULLET_RE.match(stripped):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                next_line = lines[j].strip()
                # If the next non-empty line is another bullet (the listItem
                # was empty) or a URL (smartlink), skip this bullet entirely.
                if (
                    not _BARE_BULLET_RE.match(next_line)
                    and not _BULLET_RE.match(lines[j])
                    and not _URL_LINE_RE.match(next_line)
                ):
                    bullets.append(next_line)
                    i = j + 1
                    continue
            i += 1
            continue

        # Empty line — keep going, the bullet list might continue after it.
        if not stripped:
            i += 1
            continue

        # Bare URL line — just noise (smartlinks, Figma proto links). Skip.
        if _URL_LINE_RE.match(stripped):
            i += 1
            continue

        # Non-bullet, non-empty, non-URL line. This is one of two things:
        #
        #   (a) A grouping sub-label that organizes the AC bullets into
        #       categories — e.g. "Agent mobile app", "Title rep web app",
        #       "Admin dashboard", "General". Larger Jira stories routinely
        #       group their ACs this way, and the labels sit *between* the AC
        #       heading and the bullets they introduce. These must NOT end the
        #       block — doing so drops every AC after the first label (the
        #       SK-2290 failure: extraction returned 0 ACs, disabling the
        #       entire coverage safety net).
        #
        #   (b) The start of a genuinely different section ("Implementation
        #       Notes", "Out of Scope") — this DOES end the AC block.
        #
        # Distinguish them: a grouping sub-label is a short heading-like line
        # that is immediately followed by more bullets and is not one of the
        # well-known post-AC section names. Anything else stops the block —
        # free-form prose mid-AC-block is uncommon enough that "stop early" is
        # safer than "capture noise".
        if (
            not _TERMINAL_AC_SECTION_RE.match(bare)
            and _looks_like_section_heading(stripped)
            and _next_nonblank_is_bullet(lines, i + 1)
        ):
            i += 1
            continue
        break

    # De-duplicate while preserving order; drop ultra-short entries.
    seen: set[str] = set()
    deduped: list[str] = []
    for b in bullets:
        key = b.lower()
        if len(b) < 3 or key in seen:
            continue
        seen.add(key)
        deduped.append(b)
    return deduped
