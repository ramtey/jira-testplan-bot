"""Tests for the third-pass code-grounding critic — the pass that softens
AC-grounding warnings when the linked repo actually implements the
behaviour the case tests.

Regression coverage is anchored on the real-world SK-2241 example: the
AC-grounding critic marked "Empty audio synthesis does not overwrite
existing cached audio" as ungrounded because AC2/AC3 don't mention empty-
buffer protection. But the linked repo has an explicit
``if audio.length === 0`` guard in ``audio-cache.service.ts``. This
critic's job is to downgrade the warning from WARN to INFO so QA doesn't
chase a false positive.
"""

from unittest.mock import AsyncMock

import pytest

from src.app.code_grounding_critic import (
    apply_code_verdicts,
    build_code_critic_user_message,
    build_code_verification_inputs,
    build_search_query,
    extract_repos,
    parse_code_verdicts,
    select_recheckable_warnings,
)
from src.app.models import TestPlan


# ─── fixtures ─────────────────────────────────────────────────────────────────


def _sk2241_plan_with_ac_critic_warning() -> TestPlan:
    """Fixture reflecting a plan whose AC critic has already run and
    added a ``critic_ac`` warning for the empty-buffer case."""
    return TestPlan(
        happy_path=[],
        edge_cases=[
            {
                "title": "Empty audio synthesis does not overwrite existing cached audio",
                "steps": [
                    "Trigger an audio synthesis for a share flow calc.",
                    "Force the TTS provider to return zero bytes.",
                    "Confirm the previous cached audio is still served.",
                ],
                "expected": "S3 cache key is not overwritten with empty payload.",
                "priority": "high",
                "covers_acs": ["SK-2241-AC2"],
                "needs_manual_verification": True,
            }
        ],
        integration_tests=[],
        regression_checklist=[],
        grounding_warnings=[
            {
                "ac_id": "SK-2241-AC2",
                "missing_element": "Empty audio synthesis does not overwrite existing cached audio",
                "explanation": (
                    "Critic pass: Neither AC2 nor AC3 mention behavior for empty synthesis "
                    "attempts or protection against overwriting cached audio with empty data."
                ),
                "source": "critic_ac",
                "severity": "warn",
            }
        ],
    )


def _sk2241_dev_infos() -> list[dict]:
    return [
        {
            "ticket_key": "SK-2241",
            "development_info": {
                "pull_requests": [
                    {
                        "title": "Ship walkthrough audio caching",
                        "status": "merged",
                        "repository": "skyslope/agent-calculator",
                        "url": "https://github.com/skyslope/agent-calculator/pull/999",
                    }
                ],
                "commits": [],
                "branches": [],
            },
        }
    ]


# ─── select_recheckable_warnings ──────────────────────────────────────────────


def test_select_recheckable_warnings_only_critic_ac_source():
    """The recheck should only target AC-critic warnings — fix-scope and
    LLM-native warnings are out of scope for this pass."""
    warnings = [
        {"ac_id": "AC1", "missing_element": "a", "source": "critic_ac"},
        {"ac_id": "AC2", "missing_element": "b", "source": "critic_scope"},
        {"ac_id": "AC3", "missing_element": "c"},  # legacy / LLM-native
    ]
    out = select_recheckable_warnings(warnings)
    assert [w["ac_id"] for w in out] == ["AC1"]


def test_select_recheckable_warnings_skips_already_downgraded():
    """A warning already flipped to info on a previous pass shouldn't be
    re-evaluated — it's already been softened once."""
    warnings = [
        {"ac_id": "AC1", "missing_element": "a", "source": "critic_ac", "severity": "info"},
        {"ac_id": "AC2", "missing_element": "b", "source": "critic_ac", "severity": "warn"},
    ]
    out = select_recheckable_warnings(warnings)
    assert [w["ac_id"] for w in out] == ["AC2"]


def test_select_recheckable_warnings_capped_at_limit():
    warnings = [
        {"ac_id": f"AC{i}", "missing_element": f"case-{i}", "source": "critic_ac"}
        for i in range(20)
    ]
    assert len(select_recheckable_warnings(warnings, limit=3)) == 3


def test_select_recheckable_warnings_drops_shapeless():
    warnings = [
        "not a dict",
        {"source": "critic_ac"},  # missing ac_id + missing_element
        {"ac_id": "AC1", "missing_element": "ok", "source": "critic_ac"},
    ]
    out = select_recheckable_warnings(warnings)
    assert [w["ac_id"] for w in out] == ["AC1"]


# ─── extract_repos ────────────────────────────────────────────────────────────


def test_extract_repos_preserves_first_seen_order_and_dedupes():
    dev_infos = [
        {
            "development_info": {
                "pull_requests": [
                    {"repository": "owner/a"},
                    {"repository": "owner/b"},
                    {"repository": "owner/a"},  # dup
                    {"repository": ""},           # skipped
                    {"repository": None},         # skipped
                ]
            }
        },
        {
            "development_info": {
                "pull_requests": [
                    {"repository": "owner/c"},
                    {"repository": "owner/b"},  # dup across tickets
                ]
            }
        },
    ]
    assert extract_repos(dev_infos) == ["owner/a", "owner/b", "owner/c"]


def test_extract_repos_missing_development_info_ok():
    assert extract_repos([{"ticket_key": "SK-1"}]) == []
    assert extract_repos([]) == []


# ─── build_search_query ───────────────────────────────────────────────────────


def test_build_search_query_drops_stopwords_and_keeps_domain_tokens():
    warning = {
        "missing_element": "Empty audio synthesis does not overwrite existing cached audio",
    }
    query = build_search_query(warning)
    # "does" and "not" are stopwords; "audio" appears twice — deduped.
    tokens = query.split()
    assert "does" not in tokens
    assert "not" not in tokens
    assert tokens.count("audio") == 1
    # Domain nouns/verbs must survive.
    assert "synthesis" in tokens
    assert "cached" in tokens or "cache" in tokens or "overwrite" in tokens


def test_build_search_query_empty_when_no_title():
    assert build_search_query({}) == ""
    assert build_search_query({"missing_element": "  "}) == ""


def test_build_search_query_caps_token_count():
    warning = {
        "missing_element": "alpha beta gamma delta epsilon zeta eta theta iota kappa",
    }
    assert len(build_search_query(warning).split()) <= 6


# ─── build_code_verification_inputs ───────────────────────────────────────────


def test_build_code_verification_inputs_pairs_warning_with_case_and_snippets():
    plan = _sk2241_plan_with_ac_critic_warning()
    warnings = list(plan.grounding_warnings)
    hits = {
        0: [
            {
                "path": "apps/server/src/services/share-flow/audio-cache.service.ts",
                "ref": "main",
                "content": "if (audio.length === 0) { throw new Error('empty'); }",
            }
        ]
    }
    inputs = build_code_verification_inputs(plan, warnings, hits)
    assert len(inputs) == 1
    entry = inputs[0]
    assert entry["ac_id"] == "SK-2241-AC2"
    assert entry["title"].startswith("Empty audio synthesis")
    # Case body copied through so the LLM sees what the tester will run.
    assert any("zero bytes" in s for s in entry["steps"])
    assert "empty" in entry["expected"].lower()
    # Snippet is preserved with path + ref so the model can cite it.
    assert entry["code_snippets"][0]["path"].endswith("audio-cache.service.ts")


def test_build_code_verification_inputs_skips_warnings_with_no_snippets():
    """No snippets → nothing to verify against; skip rather than call the
    LLM with an empty payload."""
    plan = _sk2241_plan_with_ac_critic_warning()
    warnings = list(plan.grounding_warnings)
    inputs = build_code_verification_inputs(plan, warnings, {})
    assert inputs == []


def test_build_code_verification_inputs_ok_when_case_not_findable():
    """If the case title was rewritten between the AC critic and this
    pass, we can still verify against the warning + snippet alone — the
    case body just comes through empty."""
    plan = TestPlan(
        happy_path=[],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
        grounding_warnings=[
            {
                "ac_id": "AC1",
                "missing_element": "ghost case",
                "explanation": "Critic pass: nope.",
                "source": "critic_ac",
                "severity": "warn",
            }
        ],
    )
    hits = {0: [{"path": "x.ts", "ref": "main", "content": "// something"}]}
    inputs = build_code_verification_inputs(plan, list(plan.grounding_warnings), hits)
    assert len(inputs) == 1
    assert inputs[0]["steps"] == []
    assert inputs[0]["expected"] == ""


# ─── build_code_critic_user_message ───────────────────────────────────────────


def test_build_code_critic_user_message_includes_snippet_content():
    """The LLM must see the actual code — that's the whole point."""
    cases = [
        {
            "warning_key": "SK-2241-AC2::Empty audio synthesis does not overwrite existing cached audio",
            "ac_id": "SK-2241-AC2",
            "title": "Empty audio synthesis does not overwrite existing cached audio",
            "steps": ["do a thing"],
            "expected": "does not overwrite",
            "critic_reason": "Critic pass: not in AC2 or AC3.",
            "code_snippets": [
                {
                    "path": "audio-cache.service.ts",
                    "ref": "main",
                    "content": "if (audio.length === 0) { throw new Error('empty'); }",
                }
            ],
        }
    ]
    message = build_code_critic_user_message(cases)
    assert "audio-cache.service.ts" in message
    assert "audio.length === 0" in message
    # The warning_key must survive so the LLM's tool call can reference it.
    assert "SK-2241-AC2::Empty audio synthesis" in message
    # The prior critic's reason is included so the model can rebut it.
    assert "Critic pass: not in AC2 or AC3." in message


# ─── parse_code_verdicts ──────────────────────────────────────────────────────


def test_parse_code_verdicts_accepts_tool_input_wrapper():
    raw = {"verdicts": [
        {"warning_key": "k1", "verdict": "implemented", "reason": "line 520."},
        {"warning_key": "k2", "verdict": "not_implemented", "reason": "no match."},
        {"warning_key": "k3", "verdict": "unclear", "reason": "truncated."},
    ]}
    out = parse_code_verdicts(raw)
    assert set(out.keys()) == {"k1", "k2", "k3"}
    assert out["k1"]["verdict"] == "implemented"


def test_parse_code_verdicts_drops_unknown_verdicts_and_shapeless_entries():
    raw = [
        "not a dict",
        {"warning_key": "", "verdict": "implemented", "reason": ""},
        {"warning_key": "k", "verdict": "maybe", "reason": ""},
        {"warning_key": "kept", "verdict": "implemented", "reason": "ok"},
    ]
    out = parse_code_verdicts(raw)
    assert list(out.keys()) == ["kept"]


def test_parse_code_verdicts_missing_input_returns_empty():
    assert parse_code_verdicts(None) == {}
    assert parse_code_verdicts({"other": "shape"}) == {}


# ─── apply_code_verdicts ──────────────────────────────────────────────────────


def test_apply_code_verdicts_downgrades_implemented_warning():
    plan = _sk2241_plan_with_ac_critic_warning()
    key = "SK-2241-AC2::Empty audio synthesis does not overwrite existing cached audio"
    verdicts = {
        key: {
            "verdict": "implemented",
            "reason": "audio-cache.service.ts:520 has an explicit empty-buffer guard.",
        }
    }
    evidence = {
        key: [
            {
                "path": "apps/server/src/services/share-flow/audio-cache.service.ts",
                "ref": "main",
                "content": "if (audio.length === 0) throw ...",
            }
        ]
    }

    downgraded = apply_code_verdicts(plan, verdicts, evidence)

    assert len(downgraded) == 1
    warning = plan.grounding_warnings[0]
    assert warning["severity"] == "info"
    assert "code implements" in warning["explanation"].lower() or "implements" in warning["explanation"].lower()
    # File anchor is attached so QA can jump straight to the source.
    assert warning["code_evidence"]["files"][0]["path"].endswith("audio-cache.service.ts")
    # The matching case's manual-verification badge is cleared alongside
    # the downgrade so the UI stops flagging it.
    assert plan.edge_cases[0]["needs_manual_verification"] is False


def test_apply_code_verdicts_leaves_not_implemented_alone():
    plan = _sk2241_plan_with_ac_critic_warning()
    key = "SK-2241-AC2::Empty audio synthesis does not overwrite existing cached audio"
    verdicts = {key: {"verdict": "not_implemented", "reason": "no guard visible."}}
    apply_code_verdicts(plan, verdicts, {})

    warning = plan.grounding_warnings[0]
    assert warning["severity"] == "warn"
    assert warning["explanation"].startswith("Critic pass:")
    # The case stays badged — the tester should still verify.
    assert plan.edge_cases[0]["needs_manual_verification"] is True


def test_apply_code_verdicts_unclear_verdict_is_noop():
    """Ambiguous evidence must NOT downgrade — false reassurance is
    worse than a false warning."""
    plan = _sk2241_plan_with_ac_critic_warning()
    key = "SK-2241-AC2::Empty audio synthesis does not overwrite existing cached audio"
    verdicts = {key: {"verdict": "unclear", "reason": "snippet too short."}}
    apply_code_verdicts(plan, verdicts, {})
    assert plan.grounding_warnings[0]["severity"] == "warn"


def test_apply_code_verdicts_ignores_non_ac_source_warnings():
    """Even if a fix-scope warning happens to share a key with a verdict,
    this critic must not touch it — it's out of scope."""
    plan = TestPlan(
        happy_path=[],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
        grounding_warnings=[
            {
                "ac_id": "AC1",
                "missing_element": "case",
                "explanation": "Fix-scope critic: out of scope.",
                "source": "critic_scope",
                "severity": "warn",
            }
        ],
    )
    apply_code_verdicts(
        plan,
        {"AC1::case": {"verdict": "implemented", "reason": "code exists."}},
        {},
    )
    # Fix-scope warning is untouched.
    assert plan.grounding_warnings[0]["severity"] == "warn"


def test_apply_code_verdicts_empty_verdicts_is_noop():
    plan = _sk2241_plan_with_ac_critic_warning()
    assert apply_code_verdicts(plan, {}) == []
    assert plan.grounding_warnings[0]["severity"] == "warn"


# ─── End-to-end integration through _run_code_grounding_critic ────────────────


@pytest.mark.asyncio
async def test_run_code_grounding_critic_downgrades_sk2241_warning(monkeypatch):
    """End-to-end: given the SK-2241 warning + a linked repo, a mocked
    GitHub client returning the empty-buffer snippet + a mocked LLM
    returning an ``implemented`` verdict must flip the warning to INFO."""
    from src.app import main as main_module
    from src.app.config import settings

    monkeypatch.setattr(settings, "github_token", "test-token", raising=False)
    monkeypatch.setattr(settings, "code_grounding_recheck_enabled", True, raising=False)

    plan = _sk2241_plan_with_ac_critic_warning()
    dev_infos = _sk2241_dev_infos()

    class _FakeGitHubClient:
        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        async def search_relevant_files(self, repo, query, max_files=3):
            self.calls.append((repo, query))
            return [
                {
                    "path": "apps/server/src/services/share-flow/audio-cache.service.ts",
                    "ref": "main",
                    "content": (
                        "if (audio.length === 0) {\n"
                        "  throw new Error('synthesized audio is empty; refusing to persist');\n"
                        "}"
                    ),
                }
            ]

    fake_client = _FakeGitHubClient()
    # Patch the constructor so the critic pulls our fake instance.
    monkeypatch.setattr(
        "src.app.github_client.GitHubClient",
        lambda *a, **kw: fake_client,
    )

    class _FakeLLM:
        async def verify_code_grounding(self, cases):
            # Assert the payload actually contains the snippet content — that's
            # the entire point of this pass.
            assert cases and "audio.length === 0" in cases[0]["code_snippets"][0]["content"]
            return {
                cases[0]["warning_key"]: {
                    "verdict": "implemented",
                    "reason": "audio-cache.service.ts contains an explicit empty-buffer guard.",
                }
            }

    await main_module._run_code_grounding_critic(_FakeLLM(), plan, dev_infos)

    assert plan.grounding_warnings[0]["severity"] == "info"
    assert plan.edge_cases[0]["needs_manual_verification"] is False
    # We hit the linked repo, not some other guess.
    assert fake_client.calls and fake_client.calls[0][0] == "skyslope/agent-calculator"


@pytest.mark.asyncio
async def test_run_code_grounding_critic_no_op_without_github_token(monkeypatch):
    """Without a GitHub token we can't even search — bail before making
    any calls, and leave warnings intact."""
    from src.app import main as main_module
    from src.app.config import settings

    monkeypatch.setattr(settings, "github_token", None, raising=False)
    monkeypatch.setattr(settings, "code_grounding_recheck_enabled", True, raising=False)

    plan = _sk2241_plan_with_ac_critic_warning()
    llm = AsyncMock()
    llm.verify_code_grounding = AsyncMock(return_value={})
    await main_module._run_code_grounding_critic(llm, plan, _sk2241_dev_infos())

    llm.verify_code_grounding.assert_not_called()
    assert plan.grounding_warnings[0]["severity"] == "warn"


@pytest.mark.asyncio
async def test_run_code_grounding_critic_no_op_when_toggle_off(monkeypatch):
    from src.app import main as main_module
    from src.app.config import settings

    monkeypatch.setattr(settings, "github_token", "test-token", raising=False)
    monkeypatch.setattr(settings, "code_grounding_recheck_enabled", False, raising=False)

    plan = _sk2241_plan_with_ac_critic_warning()
    llm = AsyncMock()
    llm.verify_code_grounding = AsyncMock(return_value={})
    await main_module._run_code_grounding_critic(llm, plan, _sk2241_dev_infos())

    llm.verify_code_grounding.assert_not_called()
    assert plan.grounding_warnings[0]["severity"] == "warn"


@pytest.mark.asyncio
async def test_run_code_grounding_critic_swallows_search_errors(monkeypatch):
    """A crashing GitHub search must not abort the request — the
    warnings simply stay at WARN severity."""
    from src.app import main as main_module
    from src.app.config import settings

    monkeypatch.setattr(settings, "github_token", "test-token", raising=False)
    monkeypatch.setattr(settings, "code_grounding_recheck_enabled", True, raising=False)

    class _ExplodingClient:
        async def search_relevant_files(self, repo, query, max_files=3):
            raise RuntimeError("gh down")

    monkeypatch.setattr(
        "src.app.github_client.GitHubClient",
        lambda *a, **kw: _ExplodingClient(),
    )

    plan = _sk2241_plan_with_ac_critic_warning()
    llm = AsyncMock()
    llm.verify_code_grounding = AsyncMock(return_value={})

    await main_module._run_code_grounding_critic(llm, plan, _sk2241_dev_infos())

    # Without any snippets there was nothing to LLM-verify — LLM never called.
    llm.verify_code_grounding.assert_not_called()
    assert plan.grounding_warnings[0]["severity"] == "warn"


@pytest.mark.asyncio
async def test_run_code_grounding_critic_swallows_llm_errors(monkeypatch):
    """LLM transport blowup must not abort the request."""
    from src.app import main as main_module
    from src.app.config import settings

    monkeypatch.setattr(settings, "github_token", "test-token", raising=False)
    monkeypatch.setattr(settings, "code_grounding_recheck_enabled", True, raising=False)

    class _FakeClient:
        async def search_relevant_files(self, repo, query, max_files=3):
            return [{"path": "x.ts", "ref": "main", "content": "// stuff"}]

    monkeypatch.setattr(
        "src.app.github_client.GitHubClient",
        lambda *a, **kw: _FakeClient(),
    )

    class _ExplodingLLM:
        async def verify_code_grounding(self, cases):
            raise RuntimeError("boom")

    plan = _sk2241_plan_with_ac_critic_warning()
    await main_module._run_code_grounding_critic(_ExplodingLLM(), plan, _sk2241_dev_infos())

    assert plan.grounding_warnings[0]["severity"] == "warn"


# ─── LLMClient default implementation is a no-op ──────────────────────────────


@pytest.mark.asyncio
async def test_default_llmclient_verify_code_grounding_is_empty():
    """Base LLMClient returns {} — providers that don't implement the
    critic (e.g. Ollama) degrade to leaving warnings intact."""
    from src.app.llm_client import OllamaClient
    client = OllamaClient()
    result = await client.verify_code_grounding([
        {
            "warning_key": "k",
            "ac_id": "AC1",
            "title": "t",
            "steps": [],
            "expected": "",
            "critic_reason": "",
            "code_snippets": [{"path": "x", "ref": "", "content": "y"}],
        }
    ])
    assert result == {}
