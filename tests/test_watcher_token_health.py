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


class TestOfflineIsNotABrokenToken:
    """Being unable to ask is not an answer.

    Offline, every check returns is_valid=False with SERVICE_UNAVAILABLE. The
    first version of this monitor read that as "the token is broken" and woke
    the user on a plane — the same failed-lookup-reported-as-fact bug it was
    built to catch.
    """

    @pytest.mark.asyncio
    async def test_unreachable_services_do_not_notify(self, monkeypatch, tmp_path):
        seen = []
        monkeypatch.setattr(queue_watcher, "TOKEN_STATE_PATH", tmp_path / "s.json")
        monkeypatch.setattr(queue_watcher, "notify",
                            lambda *a, **k: seen.append(a))
        _patch(monkeypatch, FakeHealth([
            FakeStatus("Jira", False, "service_unavailable", "Cannot connect. Check network."),
            FakeStatus("Figma", False, "service_unavailable", "Cannot connect. Check network."),
        ]))
        await queue_watcher.check_tokens_once()
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_real_failure_still_notifies_while_offline_ones_do_not(
        self, monkeypatch, tmp_path
    ):
        seen = []
        monkeypatch.setattr(queue_watcher, "TOKEN_STATE_PATH", tmp_path / "s.json")
        monkeypatch.setattr(queue_watcher, "notify", lambda t, m, **k: seen.append(m))
        _patch(monkeypatch, FakeHealth([
            FakeStatus("Jira", False, "service_unavailable", "Cannot connect."),
            FakeStatus("Figma", False, "expired", "Token has expired."),
        ]))
        await queue_watcher.check_tokens_once()
        assert len(seen) == 1
        assert "Figma" in seen[0] and "Jira" not in seen[0]

    @pytest.mark.asyncio
    async def test_being_offline_does_not_overwrite_a_known_good_state(
        self, monkeypatch, tmp_path
    ):
        import json as _json
        state = tmp_path / "s.json"
        state.write_text(_json.dumps({"last_checked_epoch": 111.0,
                                      "health": {"Figma": True}}))
        monkeypatch.setattr(queue_watcher, "TOKEN_STATE_PATH", state)
        monkeypatch.setattr(queue_watcher, "notify", lambda *a, **k: None)
        _patch(monkeypatch, FakeHealth([
            FakeStatus("Figma", False, "service_unavailable", "Cannot connect.")]))
        await queue_watcher.check_tokens_once()
        after = _json.loads(state.read_text())
        # Still believed good, and the clock did not advance — so the next
        # sweep retries rather than buying six hours of silence by being offline.
        assert after["health"]["Figma"] is True
        assert after["last_checked_epoch"] == 111.0

    @pytest.mark.asyncio
    async def test_an_unconfigured_optional_token_is_not_an_alert(
        self, monkeypatch, tmp_path
    ):
        seen = []
        monkeypatch.setattr(queue_watcher, "TOKEN_STATE_PATH", tmp_path / "s.json")
        monkeypatch.setattr(queue_watcher, "notify", lambda *a, **k: seen.append(a))
        _patch(monkeypatch, FakeHealth([
            FakeStatus("Figma", False, "missing", "Not configured (optional).")]))
        await queue_watcher.check_tokens_once()
        assert seen == []


# ---------------------------------------------------------------------------
# GitHub: probe a real repo, and hold the sweep while it is refused
# ---------------------------------------------------------------------------

class FakeGitHubHealth:
    def __init__(self, status):
        self.status = status

    async def validate_github_token(self):
        return self.status


@pytest.fixture
def hold_env(monkeypatch, tmp_path):
    from src.app.config import settings
    monkeypatch.setattr(settings, "github_token", "github_pat_x", raising=False)
    monkeypatch.setattr(settings, "watch_hold_without_github", True, raising=False)
    monkeypatch.setattr(queue_watcher, "TOKEN_STATE_PATH", tmp_path / "state.json")
    sent = []
    monkeypatch.setattr(queue_watcher, "notify", lambda title, msg, **k: sent.append(title))
    return sent


@pytest.mark.asyncio
async def test_github_hold_alerts_once_then_releases(monkeypatch, hold_env):
    refused = FakeStatus("GitHub", False, "insufficient_permissions",
                         "The GitHub token can no longer read skyslope/digisign3-ui (404).")
    _patch_github(monkeypatch, refused)
    assert "can no longer read" in await queue_watcher._github_hold_reason()
    assert "can no longer read" in await queue_watcher._github_hold_reason()
    assert hold_env == ["Test Plan Bot: plans on hold"]  # once, not every sweep

    _patch_github(monkeypatch, FakeStatus("GitHub", True))
    assert await queue_watcher._github_hold_reason() is None
    assert hold_env[-1] == "Test Plan Bot"  # the all-clear


@pytest.mark.asyncio
async def test_an_unreachable_github_is_not_a_hold(monkeypatch, hold_env):
    """Offline says nothing about access; never hold on a guess."""
    _patch_github(monkeypatch, FakeStatus("GitHub", False, "service_unavailable", "timed out"))
    assert await queue_watcher._github_hold_reason() is None
    assert hold_env == []


@pytest.mark.asyncio
async def test_hold_can_be_switched_off(monkeypatch, hold_env):
    from src.app.config import settings
    monkeypatch.setattr(settings, "watch_hold_without_github", False)
    _patch_github(monkeypatch, FakeStatus("GitHub", False, "insufficient_permissions", "x"))
    assert await queue_watcher._github_hold_reason() is None


def _patch_github(monkeypatch, status):
    import src.app.token_service as ts
    monkeypatch.setattr(ts, "TokenHealthService", lambda: FakeGitHubHealth(status))


# --- the probe itself: /user answering 200 is not enough -------------------

import httpx  # noqa: E402

from src.app.token_service import TokenErrorType, TokenHealthService  # noqa: E402


def _github(monkeypatch, repo_status, repo_body):
    from src.app import token_service as ts
    monkeypatch.setattr(ts.settings, "github_token", "github_pat_x", raising=False)
    monkeypatch.setattr(ts.settings, "github_healthcheck_repo", "skyslope/digisign3-ui", raising=False)

    async def _get(self, url, **kwargs):
        req = httpx.Request("GET", url)
        if url.endswith("/user"):
            return httpx.Response(200, json={"login": "bot"}, request=req)
        return httpx.Response(repo_status, json=repo_body, request=req)
    monkeypatch.setattr(httpx.AsyncClient, "get", _get)


@pytest.mark.asyncio
async def test_ip_allow_list_is_named_as_such(monkeypatch):
    _github(monkeypatch, 403, {"message": "Although you appear to have the correct authorization "
                                          "credentials, the `skyslope` organization has an IP allow list enabled"})
    st = await TokenHealthService().validate_github_token()
    assert not st.is_valid and st.error_type == TokenErrorType.INSUFFICIENT_PERMISSIONS
    assert "IP allow list" in st.error_message and "VPN" in st.error_message


@pytest.mark.asyncio
async def test_lost_repo_access_fails_even_though_user_answers(monkeypatch):
    _github(monkeypatch, 404, {"message": "Not Found"})
    st = await TokenHealthService().validate_github_token()
    assert not st.is_valid and "can no longer read skyslope/digisign3-ui (404)" in st.error_message


@pytest.mark.asyncio
async def test_readable_repo_is_healthy_and_says_what_it_read(monkeypatch):
    _github(monkeypatch, 200, {"full_name": "skyslope/digisign3-ui"})
    st = await TokenHealthService().validate_github_token()
    assert st.is_valid and st.details["repo_access"] == "reads skyslope/digisign3-ui"


@pytest.mark.asyncio
async def test_no_repo_to_probe_says_it_did_not_check(monkeypatch):
    _github(monkeypatch, 200, {})
    from src.app import token_service as ts
    monkeypatch.setattr(ts.settings, "github_healthcheck_repo", None)
    monkeypatch.setattr(ts.settings, "deploy_state_sources", {})
    monkeypatch.setattr(ts.settings, "ld_flag_sources", {})
    st = await TokenHealthService().validate_github_token()
    assert st.is_valid and st.details["repo_access"].startswith("not checked")
