"""
Parse Atlassian Document Format (ADF) to extract plain text.

ADF is a nested JSON structure that Jira Cloud uses for rich text fields.
This module extracts readable text from that structure.
"""


def extract_text_from_adf(adf_content: dict | str | None) -> str:
    """
    Extract plain text from Jira's Atlassian Document Format.

    Args:
        adf_content: The ADF JSON structure, plain string, or None

    Returns:
        Extracted plain text, or empty string if content is None/empty
    """
    if adf_content is None:
        return ""

    # If it's already a string, return it
    if isinstance(adf_content, str):
        return adf_content.strip()

    # If it's not a dict, try converting and returning
    if not isinstance(adf_content, dict):
        return str(adf_content).strip()

    # Extract text from ADF structure
    text_parts = []
    _extract_text_recursive(adf_content, text_parts)
    return "\n".join(text_parts).strip()


def _extract_text_recursive(node: dict | list | str, text_parts: list[str]) -> None:
    """
    Recursively traverse ADF structure and extract text content.

    Args:
        node: Current ADF node (dict, list, or string)
        text_parts: Accumulator list for extracted text
    """
    if isinstance(node, str):
        text_parts.append(node)
        return

    if isinstance(node, list):
        for item in node:
            _extract_text_recursive(item, text_parts)
        return

    if not isinstance(node, dict):
        return

    # Extract text from "text" field if present
    if "text" in node:
        text_parts.append(node["text"])

    # Handle specific node types that might need special formatting
    node_type = node.get("type")

    # Add line break after paragraphs and headings
    if node_type in ("paragraph", "heading", "codeBlock"):
        # Process content first
        if "content" in node:
            _extract_text_recursive(node["content"], text_parts)
        # Add line break after
        text_parts.append("")

    # Process ordered/unordered lists
    elif node_type in ("bulletList", "orderedList"):
        if "content" in node:
            _extract_text_recursive(node["content"], text_parts)
        text_parts.append("")

    # Process list items with bullet points
    elif node_type == "listItem":
        text_parts.append("• ")
        if "content" in node:
            _extract_text_recursive(node["content"], text_parts)

    # For other node types, just process content
    elif "content" in node:
        _extract_text_recursive(node["content"], text_parts)
