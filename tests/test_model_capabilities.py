"""Model-capability rules — the guards a model bump has to get right.

These assert against Anthropic's published per-model API behaviour, not
against what the client currently sends: the whole point of the module is
that the request shape has to follow the model, and getting it wrong is a
hard 400 rather than a degraded answer.
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.app.model_capabilities import (
    DEFAULT_CLAUDE_MODEL,
    output_budget,
    parse_model,
    supports_effort,
    supports_temperature,
    thinks_by_default,
)


class TestParseModel:
    """Both id styles Anthropic ships have to compare cleanly."""

    def test_dated_id(self):
        assert parse_model("claude-opus-4-5-20251101") == ("opus", (4, 5))

    def test_bare_id_has_implicit_minor_zero(self):
        assert parse_model("claude-opus-5") == ("opus", (5, 0))

    def test_point_release(self):
        assert parse_model("claude-fable-5-1") == ("fable", (5, 1))

    def test_non_claude_model(self):
        assert parse_model("llama3.1") is None
        assert parse_model(None) is None


class TestTemperature:
    """Removed in Opus 4.7 and never came back — sending it is a 400."""

    def test_accepted_through_opus_4_6(self):
        assert supports_temperature("claude-opus-4-5-20251101")
        assert supports_temperature("claude-opus-4-6")

    def test_rejected_from_opus_4_7_on(self):
        assert not supports_temperature("claude-opus-4-7")
        assert not supports_temperature("claude-opus-4-8")
        assert not supports_temperature("claude-opus-5")

    def test_rejected_on_sonnet_5_but_accepted_on_4_6(self):
        assert supports_temperature("claude-sonnet-4-6")
        assert not supports_temperature("claude-sonnet-5")

    def test_haiku_4_5_still_takes_it(self):
        assert supports_temperature("claude-haiku-4-5")

    def test_unknown_model_omits_it(self):
        # A model newer than this file: a duller request beats a 400.
        assert not supports_temperature("claude-opus-6")


class TestEffort:
    def test_opus_from_4_5(self):
        assert supports_effort("claude-opus-4-5-20251101")
        assert supports_effort("claude-opus-5")

    def test_sonnet_4_5_and_haiku_reject_it(self):
        assert not supports_effort("claude-sonnet-4-5-20250929")
        assert not supports_effort("claude-haiku-4-5")

    def test_sonnet_from_4_6(self):
        assert supports_effort("claude-sonnet-4-6")


class TestThinkingBudget:
    def test_pre_5_does_not_think_unless_asked(self):
        assert not thinks_by_default("claude-opus-4-5-20251101")
        assert not thinks_by_default("claude-opus-4-8")

    def test_5_and_later_think_by_default(self):
        assert thinks_by_default("claude-opus-5")
        assert thinks_by_default("claude-sonnet-5")

    def test_unknown_model_reserves_headroom(self):
        # Unused headroom is free; missing headroom truncates the answer.
        assert thinks_by_default("claude-opus-6")

    def test_budget_only_grows_where_thinking_is_on(self):
        assert output_budget("claude-opus-4-5-20251101", 256, 4096) == 256
        assert output_budget("claude-opus-5", 256, 4096) == 4352


class TestDefaultModel:
    def test_default_is_consistent_with_its_own_rules(self):
        # The guards have to classify the shipped default correctly, or every
        # call goes out with a field the model rejects.
        assert not supports_temperature(DEFAULT_CLAUDE_MODEL)
        assert supports_effort(DEFAULT_CLAUDE_MODEL)
        assert thinks_by_default(DEFAULT_CLAUDE_MODEL)
