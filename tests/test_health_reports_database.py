"""`GET /health` has to fail when the database is unreachable.

The endpoint used to be `return {"status": "ok"}` — a literal that could not
fail. On 2026-09-18 Atlas was unreachable for hours: `/health` answered 200
`ok` while `/runs/by-ticket/{key}` hung ten seconds and returned 503. These
tests exist so that combination cannot come back, and they assert the *status
code* as much as the body, because that is what a monitor reads.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from src.app.main import app

client = TestClient(app)


def _db_that(command) -> MagicMock:
    db = MagicMock()
    db.command = command
    return db


def test_reachable_database_reports_ok():
    db = _db_that(AsyncMock(return_value={"ok": 1}))
    with patch("src.app.main.get_db", return_value=db):
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": {"status": "ok"}}
    db.command.assert_awaited_once_with("ping")


def test_unreachable_database_is_a_503_naming_the_error():
    """The real 2026-09-18 failure: server selection times out."""
    from pymongo.errors import ServerSelectionTimeoutError

    db = _db_that(AsyncMock(side_effect=ServerSelectionTimeoutError("No servers found")))
    with patch("src.app.main.get_db", return_value=db):
        response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"]["status"] == "unreachable"
    # Naming the exception is the point — "degraded" alone sends whoever is
    # paged looking through the whole app for it.
    assert "ServerSelectionTimeoutError" in body["database"]["error"]


def test_a_hanging_ping_is_bounded_rather_than_waited_out():
    """A health check that blocks for the client's full 10s selection timeout
    is its own failure. The ping gets a much shorter budget."""

    async def _never_answers(*_args, **_kwargs):
        await asyncio.sleep(30)

    with patch("src.app.main._HEALTH_PING_TIMEOUT_SECONDS", 0.05):
        with patch("src.app.main.get_db", return_value=_db_that(_never_answers)):
            response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["database"]["status"] == "unreachable"
    assert "exceeded" in response.json()["database"]["error"]


def test_unconstructable_client_is_also_unreachable():
    """A missing MONGODB_URI kills the app exactly as thoroughly as a dead
    cluster, so `get_db()` raising must not 500 the health check."""
    with patch("src.app.main.get_db", side_effect=RuntimeError("MONGODB_URI is not set")):
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["database"]["status"] == "unreachable"
    assert "MONGODB_URI" in response.json()["database"]["error"]
