from __future__ import annotations

from typing import ClassVar

from src.app.db.base import DocumentBase


class TicketWalkthrough(DocumentBase):
    """Human-authored "how to test this" content for a ticket: a Loom link,
    zero or more screenshots (each uploaded to Jira as an attachment), and
    free-text setup/repro notes.

    Deliberately keyed by ``ticket_key`` (not by plan/run) and stored apart from
    the LLM-generated plan body, so re-generating a plan never wipes the Loom or
    notes a test planner attached. One document per ticket; posting/regeneration
    reuse it.

    ``screenshots`` stays a JSON-encoded *string* of ``{"filename", "url"}``
    entries rather than becoming a native array. Mongo would happily store the
    array, but the frontend and the pass-to-UAT comment builder both parse this
    field as a JSON string today, and changing its shape is a frontend change
    unrelated to the database swap. Converting it is a clean follow-up.
    """

    __collection__: ClassVar[str] = "ticket_walkthroughs"

    ticket_key: str
    loom_url: str | None = None
    screenshots: str | None = None
    notes: str | None = None
