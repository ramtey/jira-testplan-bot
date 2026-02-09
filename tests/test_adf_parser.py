"""
Test ADF parser functionality, especially strikethrough text handling.
"""

from src.app.adf_parser import extract_text_from_adf


def test_strikethrough_text_is_ignored():
    """Test that strikethrough text is excluded from extracted text."""
    adf_content = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "Keep this text"
                    },
                    {
                        "type": "text",
                        "text": " ",
                    },
                    {
                        "type": "text",
                        "text": "Remove this text",
                        "marks": [
                            {
                                "type": "strike"
                            }
                        ]
                    },
                    {
                        "type": "text",
                        "text": " and keep this"
                    }
                ]
            }
        ]
    }

    result = extract_text_from_adf(adf_content)

    # Should include non-strikethrough text
    assert "Keep this text" in result
    assert "and keep this" in result

    # Should NOT include strikethrough text
    assert "Remove this text" not in result

    print("✓ Strikethrough text is properly ignored")


def test_normal_text_without_strikethrough():
    """Test that normal text without strikethrough works as before."""
    adf_content = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "This is normal text"
                    }
                ]
            }
        ]
    }

    result = extract_text_from_adf(adf_content)
    assert "This is normal text" in result
    print("✓ Normal text extraction works correctly")


def test_multiple_marks_including_strikethrough():
    """Test text with multiple marks including strikethrough."""
    adf_content = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "Bold text",
                        "marks": [
                            {
                                "type": "strong"
                            }
                        ]
                    },
                    {
                        "type": "text",
                        "text": " ",
                    },
                    {
                        "type": "text",
                        "text": "Strikethrough bold text",
                        "marks": [
                            {
                                "type": "strong"
                            },
                            {
                                "type": "strike"
                            }
                        ]
                    }
                ]
            }
        ]
    }

    result = extract_text_from_adf(adf_content)

    # Should include bold text (non-strikethrough)
    assert "Bold text" in result

    # Should NOT include strikethrough text even if it has other marks
    assert "Strikethrough bold text" not in result

    print("✓ Strikethrough with multiple marks is properly ignored")


def test_requirement_example():
    """Test realistic example: requirement with deprecated feature."""
    adf_content = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "User should be able to:"
                    }
                ]
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
                                        "text": "Upload profile picture"
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Export data to PDF",
                                        "marks": [
                                            {
                                                "type": "strike"
                                            }
                                        ]
                                    },
                                    {
                                        "type": "text",
                                        "text": " (removed - out of scope)"
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Change password"
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
    }

    result = extract_text_from_adf(adf_content)

    # Should include active requirements
    assert "Upload profile picture" in result
    assert "Change password" in result

    # Should NOT include the strikethrough requirement
    assert "Export data to PDF" not in result

    # Should still include the note about removal
    assert "(removed - out of scope)" in result

    print("✓ Realistic requirement example works correctly")
    print(f"\nExtracted text:\n{result}")


if __name__ == "__main__":
    test_strikethrough_text_is_ignored()
    test_normal_text_without_strikethrough()
    test_multiple_marks_including_strikethrough()
    test_requirement_example()
    print("\n✅ All ADF parser tests passed!")
