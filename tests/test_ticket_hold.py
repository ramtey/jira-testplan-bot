"""Tests for the QA hold endpoint's reason validation and response shaping.

The reason check runs before any DB access, so it's testable without a
database; _serialize_hold is likewise pure. The read/write round-trip is
exercised against a real Postgres, not here.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.app.db.models.ticket_hold import HOLD_REASONS
from src.app.main import _serialize_hold, put_ticket_hold
from src.app.models import TicketHoldRequest


def test_unknown_reason_is_rejected_before_any_write():
    """A typo'd reason must 400 rather than land an unreadable hold on the ticket."""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            put_ticket_hold("SK-1", TicketHoldRequest(reason="waiting-for-something"))
        )
    assert exc.value.status_code == 400
    # The message enumerates the valid codes so a client can self-correct.
    assert "code-review" in exc.value.detail


def test_empty_reason_is_rejected():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(put_ticket_hold("SK-1", TicketHoldRequest(reason="")))
    assert exc.value.status_code == 400


def test_reason_codes_cover_the_frontend_options():
    """The hook's HOLD_REASONS list mirrors this set; drift breaks the picker."""
    assert HOLD_REASONS == {
        "code-review",
        "dependency",
        "design-pm",
        "environment",
        "other",
    }


def test_serialize_hold_reports_not_held_for_a_missing_row():
    assert _serialize_hold(None) == {
        "held": False,
        "reason": None,
        "note": None,
        "held_since": None,
    }


def test_serialize_hold_exposes_created_at_as_held_since():
    """`held_since` is the row's creation time — editing a hold must not reset it,
    so the UI's "on hold since" stamp tracks when testing actually stopped."""
    created = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    updated = datetime(2026, 9, 5, 9, 30, tzinfo=timezone.utc)
    row = SimpleNamespace(
        reason="dependency",
        note="waiting on SK-1180",
        created_at=created,
        updated_at=updated,
    )
    out = _serialize_hold(row)
    assert out["held"] is True
    assert out["reason"] == "dependency"
    assert out["note"] == "waiting on SK-1180"
    assert out["held_since"] == created.isoformat()
    assert out["updated_at"] == updated.isoformat()
