"""
Analyze Jira issue descriptions for quality and completeness.

Detects when descriptions are missing, too short, or lack key testing information.
"""

from dataclasses import dataclass


@dataclass
class DescriptionAnalysis:
    """Analysis results for a Jira issue description."""

    has_description: bool
    is_weak: bool
    warnings: list[str]
    char_count: int
    word_count: int


def analyze_description(description: str | None) -> DescriptionAnalysis:
    """
    Analyze the quality of a Jira issue description.

    Args:
        description: The extracted plain text description

    Returns:
        Analysis results including warnings and quality flags
    """
    warnings = []

    # Handle missing description
    if not description or not description.strip():
        return DescriptionAnalysis(
            has_description=False,
            is_weak=True,
            warnings=["No description provided in Jira ticket"],
            char_count=0,
            word_count=0,
        )

    # Calculate metrics
    clean_text = description.strip()
    char_count = len(clean_text)
    word_count = len(clean_text.split())

    # Check if description is too short
    is_weak = False
    if char_count < 50:
        warnings.append(
            f"Description is very short ({char_count} characters). "
            "More detail may be needed for comprehensive test planning."
        )
        is_weak = True
    elif word_count < 10:
        warnings.append(
            f"Description contains only {word_count} words. "
            "Consider providing more context."
        )
        is_weak = True

    # Check for common quality indicators
    lower_text = clean_text.lower()

    # Check for acceptance criteria
    has_ac = any(
        keyword in lower_text
        for keyword in [
            "acceptance criteria",
            "ac:",
            "acceptance:",
            "should:",
            "must:",
            "given",
            "when",
            "then",
        ]
    )

    if not has_ac and char_count > 50:
        warnings.append(
            "No explicit acceptance criteria (AC) detected. "
            "You may need to provide testing context manually."
        )

    # Check for test-related keywords
    has_test_info = any(
        keyword in lower_text
        for keyword in [
            "test",
            "verify",
            "validate",
            "ensure",
            "check",
            "expected",
            "behavior",
        ]
    )

    if not has_test_info and char_count > 50:
        warnings.append(
            "No testing or validation keywords found. "
            "Consider what behaviors need verification."
        )

    return DescriptionAnalysis(
        has_description=True,
        is_weak=is_weak or len(warnings) > 0,
        warnings=warnings,
        char_count=char_count,
        word_count=word_count,
    )
