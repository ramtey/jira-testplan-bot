"""The weekly model-bump check: which model counts as newer.

Ordering is by parsed version, not release date — the capability table in
model_capabilities.py is keyed on version, and Anthropic publishes dated
snapshots of older models after newer ones ship.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.check_for_newer_model import newer_model


def m(model_id, name=None):
    return {"id": model_id, "display_name": name or model_id}


class TestNewerModel:
    def test_finds_a_higher_major_in_the_same_family(self):
        found = newer_model("claude-opus-5", [m("claude-opus-5"), m("claude-opus-6")])
        assert found["id"] == "claude-opus-6"

    def test_finds_a_higher_minor(self):
        found = newer_model("claude-opus-4-6", [m("claude-opus-4-6"), m("claude-opus-4-8")])
        assert found["id"] == "claude-opus-4-8"

    def test_none_when_already_newest(self):
        assert newer_model("claude-opus-5", [m("claude-opus-5"), m("claude-opus-4-8")]) is None

    def test_ignores_other_families(self):
        # A newer Sonnet is not a reason to change which Opus the bot runs.
        assert newer_model("claude-opus-5", [m("claude-sonnet-5"), m("claude-haiku-4-5")]) is None

    def test_picks_the_highest_when_several_are_newer(self):
        found = newer_model("claude-opus-4-6", [
            m("claude-opus-4-7"), m("claude-opus-5"), m("claude-opus-4-8")])
        assert found["id"] == "claude-opus-5"

    def test_a_dated_snapshot_of_an_older_model_is_not_newer(self):
        # Published later, older version. Date ordering would get this wrong.
        assert newer_model(
            "claude-opus-5", [m("claude-opus-4-5-20251101"), m("claude-opus-5")]) is None

    def test_an_unparseable_pin_proposes_nothing(self):
        # Ollama, or a model id shape we have never seen. Silence beats a
        # confident wrong bump.
        assert newer_model("llama3.1", [m("claude-opus-6")]) is None

    def test_junk_entries_do_not_crash_the_scan(self):
        found = newer_model("claude-opus-5", [{}, {"id": ""}, m("claude-opus-6")])
        assert found["id"] == "claude-opus-6"
