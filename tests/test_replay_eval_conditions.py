"""The replay eval has to know what each run was shown.

Both behaviours here exist to protect a number, not a user. The eval's whole
claim is "the same corpus, one variable" — and two things quietly break that:

  * A transient context failure. SK-2239 came back with 0 pull requests on a
    dev-status ReadTimeout during the 2026-09-16 pre-flight, and with 38 on a
    retry a minute later. Generated from the first fetch, its plan has no code
    to ground anything in and scores near zero; generated from the second, it
    scores normally. Left alone, that lands in the measured run-to-run
    variance, and a noise floor inflated that way sets the gate threshold so
    loose that real regressions pass through it.

  * Context that differed between the two runs for environmental reasons —
    a spent Figma quota, a Confluence page that would not load, a critic that
    could not run. That is not model variance and must not be read as a win
    or a loss for whatever change is being tested.
"""
import json

import pytest

from evals import replay_bounces as rb


class FakeJira:
    """Returns each queued payload in turn, counting calls."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    async def get_issue(self, key):
        self.calls += 1
        return self.payloads[min(self.calls - 1, len(self.payloads) - 1)]


def identity(payload):
    return payload


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def _instant(_seconds):
        return None
    monkeypatch.setattr(rb.asyncio, "sleep", _instant)


class TestATransientContextFailureIsRetried:
    @pytest.mark.asyncio
    async def test_a_good_fetch_is_not_retried(self):
        jira = FakeJira([{"dev_status_unavailable": False, "prs": 38}])
        serialized, retried = await rb.fetch_with_context(jira, "SK-2239", identity)
        assert jira.calls == 1
        assert retried is False
        assert serialized["prs"] == 38

    @pytest.mark.asyncio
    async def test_a_timed_out_fetch_is_retried_and_recovers(self):
        """The SK-2239 case: 0 PRs, then 38."""
        jira = FakeJira([
            {"dev_status_unavailable": True, "prs": 0},
            {"dev_status_unavailable": False, "prs": 38},
        ])
        serialized, retried = await rb.fetch_with_context(jira, "SK-2239", identity)
        assert jira.calls == 2
        assert retried is True
        assert serialized["prs"] == 38
        assert not serialized["dev_status_unavailable"]

    @pytest.mark.asyncio
    async def test_a_standing_outage_is_handed_back_for_exclusion(self):
        """Still broken after the retry: the caller must be able to tell, so
        it can exclude rather than score a plan written with no code."""
        jira = FakeJira([{"dev_status_unavailable": True, "prs": 0}])
        serialized, retried = await rb.fetch_with_context(jira, "SK-2239", identity)
        assert jira.calls == 2
        assert retried is True
        assert serialized["dev_status_unavailable"] is True


def _write(d, key, context):
    (d / "plans").mkdir(parents=True, exist_ok=True)
    payload = {"ticket_key": key}
    if context is not None:
        payload["_context"] = context
    (d / "plans" / f"{key}.json").write_text(json.dumps(payload))


BASE = {"had_figma": True, "prs_at_cutoff": 3, "context_gaps": [],
        "critics_unavailable": [], "model": "claude-opus-5"}


class TestConditionParity:
    def test_matching_conditions_say_so(self, tmp_path, capsys):
        a, b = tmp_path / "a", tmp_path / "b"
        _write(a, "SK-1", BASE)
        _write(b, "SK-1", BASE)
        rb.report_condition_parity(a, b, ["SK-1"])
        assert "conditions match" in capsys.readouterr().out

    def test_a_missing_design_is_not_model_variance(self, tmp_path, capsys):
        a, b = tmp_path / "a", tmp_path / "b"
        _write(a, "SK-1", BASE)
        _write(b, "SK-1", {**BASE, "had_figma": False})
        rb.report_condition_parity(a, b, ["SK-1"])
        out = capsys.readouterr().out
        assert "DIFFERENT CONDITIONS" in out
        assert "figma: True -> False" in out

    def test_unequal_code_context_is_flagged(self, tmp_path, capsys):
        a, b = tmp_path / "a", tmp_path / "b"
        _write(a, "SK-1", BASE)
        _write(b, "SK-1", {**BASE, "prs_at_cutoff": 0})
        rb.report_condition_parity(a, b, ["SK-1"])
        assert "prs: 3 -> 0" in capsys.readouterr().out

    def test_an_unreadable_link_counts_as_a_different_run(self, tmp_path, capsys):
        a, b = tmp_path / "a", tmp_path / "b"
        _write(a, "SK-1", BASE)
        _write(b, "SK-1", {**BASE, "context_gaps": ["1 of 2 Confluence pages could not be read"]})
        rb.report_condition_parity(a, b, ["SK-1"])
        assert "gaps: 0 -> 1" in capsys.readouterr().out

    def test_plans_from_before_this_existed_are_unknown_not_equal(self, tmp_path, capsys):
        """The 2026-09-14 baseline has no _context. Absence is not parity."""
        a, b = tmp_path / "a", tmp_path / "b"
        _write(a, "SK-1", None)
        _write(b, "SK-1", BASE)
        rb.report_condition_parity(a, b, ["SK-1"])
        out = capsys.readouterr().out
        assert "conditions unrecorded" in out
        assert "parity unverified" in out
        assert "conditions match" not in out


class TestRewindLeaks:
    """Two ways the answer survived the rewind, found building the Forms corpus."""

    def test_a_bounce_a_second_before_its_complaint_is_removed(self):
        # The bot moves the status ~1s before posting the comment, and
        # bounce_history pairs that comment's text with the bounce.
        cutoff = rb._iso("2026-09-16T13:38:43.612-07:00")
        payload = {"bounce_history": [
            {"timestamp": "2026-09-16T13:38:42.500-07:00", "reason": "the complaint"},
            {"timestamp": "2026-09-10T09:00:00-07:00", "reason": "an older bounce"},
        ]}
        out, removed = rb.rewind(payload, cutoff)
        assert [b["reason"] for b in out["bounce_history"]] == ["an older bounce"]
        assert removed["bounces"] == 1

    def test_a_fix_merged_before_the_comment_is_removed_when_the_row_is_dated_earlier(self):
        # SK-2662: the fix merged at 11:56, QA wrote it up at 13:38. The row is
        # dated before the merge, and the matched comment must not move the
        # cutoff past it.
        row_cutoff = rb._iso("2026-09-16T11:55:00-07:00")
        payload = {"comments": [{"created": "2026-09-16T13:38:43-07:00",
                                 "body": "shouldOverride true is coerced"}]}
        comment = rb.reason_cutoff(payload, row_cutoff, "shouldOverride true is coerced")
        cutoff = min(row_cutoff, comment)
        assert cutoff == row_cutoff
        dev = {"development_info": {"pull_requests": [
            {"merged_at": "2026-09-16T11:56:00-07:00"}]}}
        _, removed = rb.rewind(dev, cutoff)
        assert removed["prs"] == 1
