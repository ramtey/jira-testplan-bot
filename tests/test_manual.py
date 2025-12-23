"""
Manual testing script for Jira integration functionality.

Run this to test ADF parsing and description analysis with dummy data
without hitting the real Jira API.
"""

from src.app.adf_parser import extract_text_from_adf
from src.app.description_analyzer import analyze_description

# Test Case 1: Empty/None description
print("=" * 60)
print("Test 1: None/Empty Description")
print("=" * 60)
result = extract_text_from_adf(None)
analysis = analyze_description(result)
print(f"Extracted text: '{result}'")
print(f"Has description: {analysis.has_description}")
print(f"Is weak: {analysis.is_weak}")
print(f"Warnings: {analysis.warnings}")
print()

# Test Case 2: Plain string description (legacy Jira format)
print("=" * 60)
print("Test 2: Plain String Description")
print("=" * 60)
plain_text = "This is a simple bug fix. Update the login button color."
result = extract_text_from_adf(plain_text)
analysis = analyze_description(result)
print(f"Extracted text: '{result}'")
print(f"Has description: {analysis.has_description}")
print(f"Is weak: {analysis.is_weak}")
print(f"Char count: {analysis.char_count}")
print(f"Word count: {analysis.word_count}")
print(f"Warnings: {analysis.warnings}")
print()

# Test Case 3: ADF format with rich content (Jira Cloud format)
print("=" * 60)
print("Test 3: ADF Format (Jira Cloud)")
print("=" * 60)
adf_content = {
    "version": 1,
    "type": "doc",
    "content": [
        {
            "type": "paragraph",
            "content": [
                {
                    "type": "text",
                    "text": "As a user, I want to be able to reset my password so that I can regain access to my account if I forget it.",
                }
            ],
        },
        {
            "type": "heading",
            "attrs": {"level": 2},
            "content": [{"type": "text", "text": "Acceptance Criteria"}],
        },
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "User can click 'Forgot Password' link on login page",
                                }
                            ],
                        }
                    ],
                },
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "System sends reset email to registered email address",
                                }
                            ],
                        }
                    ],
                },
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Reset link expires after 24 hours",
                                }
                            ],
                        }
                    ],
                },
            ],
        },
    ],
}
result = extract_text_from_adf(adf_content)
analysis = analyze_description(result)
print(f"Extracted text:\n{result}")
print(f"\nHas description: {analysis.has_description}")
print(f"Is weak: {analysis.is_weak}")
print(f"Char count: {analysis.char_count}")
print(f"Word count: {analysis.word_count}")
print(f"Warnings: {analysis.warnings}")
print()

# Test Case 4: Very short description (should trigger warning)
print("=" * 60)
print("Test 4: Very Short Description")
print("=" * 60)
short_text = "Fix bug"
result = extract_text_from_adf(short_text)
analysis = analyze_description(result)
print(f"Extracted text: '{result}'")
print(f"Has description: {analysis.has_description}")
print(f"Is weak: {analysis.is_weak}")
print(f"Char count: {analysis.char_count}")
print(f"Word count: {analysis.word_count}")
print(f"Warnings: {analysis.warnings}")
print()

# Test Case 5: Description without acceptance criteria
print("=" * 60)
print("Test 5: No Acceptance Criteria")
print("=" * 60)
no_ac_text = """
The dashboard is loading slowly for users with large datasets.
This is impacting user experience and causing complaints from customers.
We need to optimize the database queries and implement caching.
"""
result = extract_text_from_adf(no_ac_text)
analysis = analyze_description(result)
print(f"Extracted text: '{result}'")
print(f"Has description: {analysis.has_description}")
print(f"Is weak: {analysis.is_weak}")
print(f"Char count: {analysis.char_count}")
print(f"Word count: {analysis.word_count}")
print(f"Warnings:")
for warning in analysis.warnings:
    print(f"  - {warning}")
print()

# Test Case 6: Well-structured description with AC and test keywords
print("=" * 60)
print("Test 6: High Quality Description")
print("=" * 60)
good_description = {
    "version": 1,
    "type": "doc",
    "content": [
        {
            "type": "paragraph",
            "content": [
                {
                    "type": "text",
                    "text": "Implement a feature to export user data to CSV format for compliance reporting.",
                }
            ],
        },
        {
            "type": "heading",
            "attrs": {"level": 3},
            "content": [{"type": "text", "text": "Acceptance Criteria:"}],
        },
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Given I am an admin user, when I click the Export button, then a CSV file should be downloaded",
                                }
                            ],
                        }
                    ],
                },
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "The CSV must include user ID, name, email, and registration date",
                                }
                            ],
                        }
                    ],
                },
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Verify that sensitive data (passwords) are excluded from the export",
                                }
                            ],
                        }
                    ],
                },
            ],
        },
        {
            "type": "heading",
            "attrs": {"level": 3},
            "content": [{"type": "text", "text": "Test Notes:"}],
        },
        {
            "type": "paragraph",
            "content": [
                {
                    "type": "text",
                    "text": "Test with datasets of varying sizes (10, 100, 1000+ users). Ensure the expected behavior is maintained across all test environments.",
                }
            ],
        },
    ],
}
result = extract_text_from_adf(good_description)
analysis = analyze_description(result)
print(f"Extracted text:\n{result}")
print(f"\nHas description: {analysis.has_description}")
print(f"Is weak: {analysis.is_weak}")
print(f"Char count: {analysis.char_count}")
print(f"Word count: {analysis.word_count}")
print(f"Warnings: {analysis.warnings if analysis.warnings else 'None - High quality!'}")
print()

print("=" * 60)
print("All tests completed!")
print("=" * 60)
