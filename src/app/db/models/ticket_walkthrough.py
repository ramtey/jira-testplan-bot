from __future__ import annotations

from sqlalchemy import Column, String, Text

from src.app.db.base import TimestampedBase
from sqlmodel import Field


class TicketWalkthrough(TimestampedBase, table=True):
    """Human-authored "how to test this" content for a ticket: a Loom link,
    zero or more screenshots (each uploaded to Jira as an attachment), and
    free-text setup/repro notes.

    Deliberately keyed by ``ticket_key`` (not by plan/run) and stored apart from
    the LLM-generated plan body, so re-generating a plan never wipes the Loom or
    notes a test planner attached. One row per ticket; posting/regeneration reuse it.

    ``screenshots`` is a JSON-encoded list of ``{"filename", "url"}`` entries
    pointing at Jira attachments on this ticket. Storing them here (rather
    than re-uploading each time) lets the pass-to-UAT comment enumerate each
    screenshot as a ``📷 <filename>`` callout identical to files attached
    from the UAT modal; the actual images render in Jira's Attachments
    panel below the comment stream.
    """

    __tablename__ = "ticket_walkthroughs"

    ticket_key: str = Field(
        sa_column=Column(String(length=64), nullable=False, unique=True, index=True)
    )
    loom_url: str | None = Field(default=None, max_length=1024, nullable=True)
    screenshots: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    notes: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
