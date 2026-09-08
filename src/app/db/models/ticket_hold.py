from __future__ import annotations

from sqlalchemy import Column, String, Text

from src.app.db.base import TimestampedBase
from sqlmodel import Field


# Canonical set of hold reasons. The code is what's stored and validated; the
# human label lives in the frontend so the wording can change without a
# migration. `other` is the escape hatch and expects a note to carry the detail.
HOLD_REASONS = frozenset(
    {
        "code-review",
        "dependency",
        "design-pm",
        "environment",
        "other",
    }
)


class TicketHold(TimestampedBase, table=True):
    """A QA-side "parked, and here's why" marker on a ticket.

    Distinct from anything Jira tracks: Jira's status says where the ticket is in
    the workflow and its `is blocked by` links say which tickets gate it, but
    neither captures "testing hasn't started because we're waiting on a code
    review". This row is that signal, owned by whoever is testing.

    Deliberately *not* keyed by user — same call as TestPlanProgress. A hold is
    a message to the rest of QA ("don't pick this up yet"), so everyone opening
    the ticket sees the same one. One row per ticket, present only while the
    hold is on: resuming deletes the row rather than flagging it inactive, so
    ``created_at`` always reads as "held since" without extra bookkeeping.
    """

    __tablename__ = "ticket_holds"

    ticket_key: str = Field(
        sa_column=Column(String(length=64), nullable=False, unique=True, index=True)
    )
    # One of HOLD_REASONS.
    reason: str = Field(sa_column=Column(String(length=64), nullable=False))
    # Free-text detail ("waiting on the migration in SK-1180 to merge").
    note: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
