"""The watcher's token-health check.

The check itself has always existed; nothing ran it. An expired Figma token
cost every plan its design context and said nothing, because each downstream
failure degraded to the same empty value a ticket with no designs produces.
"""

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.app.services import queue_watcher


@dataclass
class FakeStatus:
    service_name: str
    is_valid: bool
    error_type: str = "expired"
    error_message: str = "Token has expired."
    help_url: str | None = None


class FakeHealth:
    def __init__(self, statuses, raises=False):
        self.statuses = statuses
        self.raises = raises

    async def validate_all_tokens(self):
        if self.raises:
            raise RuntimeError("network down")
        return self.statuses


@pytest.fixture(autouse=True)
def _clear_state():
    queue_watcher._token_health.clear()
    yield
    queue_watcher._token_health.clear()


def _patch(monkeypatch, health):
    import src.app.token_service as ts
    monkeypatch.setattr(ts, "TokenHealthService", lambda: health)


@pytest.mark.asyncio
async def test_a_broken_token_is_logged_at_error(monkeypatch, caplog):
    _patch(monkeypatch, FakeHealth([FakeStatus("Figma", False)]))
    with caplog.at_level(logging.ERROR):
        await queue_watcher.check_tokens_once()
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert messages, "a dead token must not pass silently"
    assert any("Figma" in m and "NOT usable" in m for m in messages)


@pytest.mark.asyncio
async def test_a_standing_failure_is_reported_every_round(monkeypatch, caplog):
    """Mentioning it once is how it gets scrolled past for a month."""
    _patch(monkeypatch, FakeHealth([FakeStatus("Figma", False)]))
    with caplog.at_level(logging.ERROR):
        await queue_watcher.check_tokens_once()
        await queue_watcher.check_tokens_once()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2


@pytest.mark.asyncio
async def test_recovery_is_reported_too(monkeypatch, caplog):
    _patch(monkeypatch, FakeHealth([FakeStatus("Figma", False)]))
    await queue_watcher.check_tokens_once()
    _patch(monkeypatch, FakeHealth([FakeStatus("Figma", True)]))
    with caplog.at_level(logging.INFO):
        await queue_watcher.check_tokens_once()
    assert any("working again" in str(r.getMessage()) for r in caplog.records)


@pytest.mark.asyncio
async def test_the_check_failing_never_costs_the_sweep(monkeypatch, caplog):
    """A token check that raises must not take the watcher down with it."""
    _patch(monkeypatch, FakeHealth([], raises=True))
    with caplog.at_level(logging.ERROR):
        assert await queue_watcher.check_tokens_once() == []


class TestItRunsWhereTheWatcherActuallyRuns:
    """launchd runs `testplan watch --once` — a fresh process every 5 minutes.

    An in-process timer would either never fire or fire every sweep, so the
    clock has to live on disk.
    """

    @pytest.mark.asyncio
    async def test_the_first_ever_check_runs(self, monkeypatch, tmp_path):
        _patch(monkeypatch, FakeHealth([FakeStatus("Jira", True)]))
        monkeypatch.setattr(queue_watcher, "TOKEN_STATE_PATH", tmp_path / "s.json")
        monkeypatch.setattr(queue_watcher, "notify", lambda *a, **k: None)
        assert len(await queue_watcher.check_tokens_if_due()) == 1

    @pytest.mark.asyncio
    async def test_a_recent_check_is_not_repeated(self, monkeypatch, tmp_path):
        state = tmp_path / "s.json"
        monkeypatch.setattr(queue_watcher, "TOKEN_STATE_PATH", state)
        monkeypatch.setattr(queue_watcher, "notify", lambda *a, **k: None)
        _patch(monkeypatch, FakeHealth([FakeStatus("Jira", True)]))
        await queue_watcher.check_tokens_if_due()
        # Second sweep, five minutes later in production terms.
        assert await queue_watcher.check_tokens_if_due() == []

    @pytest.mark.asyncio
    async def test_an_old_check_runs_again(self, monkeypatch, tmp_path):
        import json as _json
        import time as _time
        state = tmp_path / "s.json"
        state.write_text(_json.dumps({
            "last_checked_epoch": _time.time() - 60 * 60 * 24,
            "health": {"Jira": True},
        }))
        monkeypatch.setattr(queue_watcher, "TOKEN_STATE_PATH", state)
        monkeypatch.setattr(queue_watcher, "notify", lambda *a, **k: None)
        _patch(monkeypatch, FakeHealth([FakeStatus("Jira", True)]))
        assert len(await queue_watcher.check_tokens_if_due()) == 1

    @pytest.mark.asyncio
    async def test_an_unwritable_state_path_does_not_break_the_sweep(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(queue_watcher, "TOKEN_STATE_PATH",
                            tmp_path / "nope" / "\0bad" / "s.json")
        monkeypatch.setattr(queue_watcher, "notify", lambda *a, **k: None)
        _patch(monkeypatch, FakeHealth([FakeStatus("Jira", True)]))
        assert len(await queue_watcher.check_tokens_once()) == 1

    @pytest.mark.asyncio
    async def test_a_broken_token_notifies(self, monkeypatch, tmp_path):
        seen = []
        monkeypatch.setattr(queue_watcher, "TOKEN_STATE_PATH", tmp_path / "s.json")
        monkeypatch.setattr(queue_watcher, "notify",
                            lambda title, msg: seen.append((title, msg)))
        _patch(monkeypatch, FakeHealth([FakeStatus("Figma", False)]))
        await queue_watcher.check_tokens_once()
        assert seen and "Figma" in seen[0][1]

    @pytest.mark.asyncio
    async def test_healthy_tokens_do_not_notify(self, monkeypatch, tmp_path):
        seen = []
        monkeypatch.setattr(queue_watcher, "TOKEN_STATE_PATH", tmp_path / "s.json")
        monkeypatch.setattr(queue_watcher, "notify",
                            lambda title, msg: seen.append((title, msg)))
        _patch(monkeypatch, FakeHealth([FakeStatus("Jira", True)]))
        await queue_watcher.check_tokens_once()
        assert seen == []
