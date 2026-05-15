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

        # Non-bullet, non-empty, non-URL line — treat as the start of a new
        # section and stop. (We don't try to recover even if it doesn't look
        # like a heading: free-form prose mid-AC-block is uncommon enough
        # that "stop early" is safer than "capture noise".)
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
