"""
Utility functions for merging resources from parent and child Jira tickets.

These functions intelligently combine Figma context and image attachments
from both the current ticket and its parent (if exists).
"""

import logging
from typing import Optional

from .models import Attachment, FigmaContext, JiraIssue

logger = logging.getLogger(__name__)


def get_figma_context(issue: JiraIssue) -> Optional[FigmaContext]:
    """
    Get Figma context, preferring child ticket but falling back to parent.

    Rationale:
    - Child ticket Figma links are usually more specific to the sub-task
    - Parent Figma links provide overall design context if child has none
    - Better to have parent context than no context at all

    Args:
        issue: The JiraIssue with potential parent

    Returns:
        FigmaContext from child or parent, or None if neither has one
    """
    # Check if current ticket has Figma in its development_info
    if issue.development_info and issue.development_info.figma_context:
        logger.info(f"Using Figma context from {issue.key}")
        return issue.development_info.figma_context

    # Fall back to parent Figma context
    if issue.parent and issue.parent.figma_context:
        logger.info(f"Using Figma context from parent {issue.parent.key}")
        return issue.parent.figma_context

    return None


def get_all_images(issue: JiraIssue, max_images: int = 4) -> list[Attachment]:
    """
    Collect images from both ticket and parent, with smart prioritization.

    Strategy:
    - Take up to 2 images from child (more specific to the sub-task)
    - Fill remaining slots with parent images (broader design context)
    - Limit total to avoid overwhelming the LLM with too many images

    Args:
        issue: The JiraIssue with potential parent
        max_images: Maximum total images to return (default: 4)

    Returns:
        List of Attachment objects, prioritizing child images first
    """
    images = []

    # Child images first (more specific to this ticket)
    if issue.attachments:
        child_limit = min(2, len(issue.attachments))
        images.extend(issue.attachments[:child_limit])
        logger.info(f"Added {child_limit} images from {issue.key}")

    # Parent images if we have room
    if issue.parent and issue.parent.attachments:
        remaining_slots = max_images - len(images)
        if remaining_slots > 0:
            parent_limit = min(remaining_slots, len(issue.parent.attachments))
            images.extend(issue.parent.attachments[:parent_limit])
            logger.info(f"Added {parent_limit} images from parent {issue.parent.key}")

    return images


def get_combined_description(issue: JiraIssue, max_parent_length: int = 1000) -> str:
    """
    Build a combined description with parent context when available.

    Args:
        issue: The JiraIssue with potential parent
        max_parent_length: Maximum characters to include from parent description

    Returns:
        Formatted string with both descriptions
    """
    parts = []

    # Current ticket description
    if issue.description:
        parts.append(f"## {issue.key} Description\n{issue.description}")

    # Parent context (if exists)
    if issue.parent:
        parts.append(f"\n## Parent Context: {issue.parent.key} - {issue.parent.summary}")

        if issue.parent.description:
            # Truncate parent description if too long
            parent_desc = issue.parent.description
            if len(parent_desc) > max_parent_length:
                parent_desc = parent_desc[:max_parent_length] + "..."
            parts.append(f"\nParent Description:\n{parent_desc}")

        # Highlight parent resources
        parent_resources = []
        if issue.parent.figma_context:
            parent_resources.append(f"Figma design: {issue.parent.figma_context.file_name}")
        if issue.parent.attachments:
            parent_resources.append(f"{len(issue.parent.attachments)} design images")

        if parent_resources:
            parts.append(f"\nParent Resources: {', '.join(parent_resources)}")

    return "\n".join(parts) if parts else ""


def should_use_parent_resources(issue: JiraIssue) -> bool:
    """
    Determine if parent resources should be included in test plan generation.

    Returns True if:
    - Issue has a parent
    - Parent has useful resources (Figma or images)

    Args:
        issue: The JiraIssue to check

    Returns:
        True if parent resources exist and should be used
    """
    if not issue.parent:
        return False

    has_figma = issue.parent.figma_context is not None
    has_images = issue.parent.attachments and len(issue.parent.attachments) > 0

    return has_figma or has_images
