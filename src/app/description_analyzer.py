"""
Analyze Jira issue descriptions for completeness — flag concrete gaps a QA tester
would have to chase the reporter/PM about (missing AC, missing repro steps, etc.).

Per-issue-type rules:
  - Bug: needs reproduction steps + expected-vs-actual behavior
  - everything else (Story/Task/Sub-task/Improvement/…): needs acceptance criteria

When the description is missing entirely, only the "Missing description" gap is
reported — the type-specific gaps would be noise.
"""

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
