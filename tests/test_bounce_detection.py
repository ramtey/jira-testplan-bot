"""
Tests for the QA/UAT bounce-back detection helpers in jira_client.

Covers:
- _is_advanced_status / _is_backward_target — status classification
- _parse_jira_timestamp — Jira's varied timestamp shapes
- _find_bounce_reason — comment-pairing heuristic (window + author bonus)
- _extract_bounce_history — end-to-end changelog walking
"""

from datetime import timezone

from src.app.jira_client import (
    _extract_bounce_history,
    _find_bounce_reason,
    _is_advanced_status,
    _is_backward_target,
    _parse_jira_timestamp,
)


# ---------- _is_advanced_status ----------

def test_is_advanced_status_matches_qa_uat_test_done_tokens():
    for name in ["In QA", "Ready for UAT", "In Testing", "Done", "Verification", "Release Candidate"]:
        assert _is_advanced_status(name), f"expected advanced: {name!r}"


def test_is_advanced_status_rejects_pre_test_states():
    for name in ["To Do", "Backlog", "Open", "In Progress", "Reopened", ""]:
        assert not _is_advanced_status(name), f"expected NOT advanced: {name!r}"


def test_is_advanced_status_handles_none():
    assert _is_advanced_status(None) is False


# ---------- _is_backward_target ----------

def test_is_backward_target_matches_known_states_case_insensitive():
    for name in ["To Do", "todo", "BACKLOG", "Open", "reopened", "In Progress"]:
        assert _is_backward_target(name), f"expected backward target: {name!r}"


def test_is_backward_target_rejects_advanced_and_unknown():
    for name in ["Done", "In Testing", "Ready for QA", "Cancelled", ""]:
        assert not _is_backward_target(name), f"expected NOT backward target: {name!r}"


def test_is_backward_target_handles_none():
    assert _is_backward_target(None) is False


# ---------- _parse_jira_timestamp ----------

def test_parse_jira_timestamp_accepts_z_suffix():
    dt = _parse_jira_timestamp("2026-05-07T12:34:56.000Z")
    assert dt is not None
    assert dt.tzinfo is not None and dt.utcoffset() == timezone.utc.utcoffset(None)


def test_parse_jira_timestamp_normalizes_unpunctuated_offset():
    # Jira's "+0000" must be coerced to "+00:00" for fromisoformat on older runtimes.
    dt = _parse_jira_timestamp("2026-05-07T12:34:56.000+0000")
    assert dt is not None
    assert dt.utcoffset() is not None
    assert dt.utcoffset().total_seconds() == 0


def test_parse_jira_timestamp_returns_none_for_invalid():
    assert _parse_jira_timestamp("not a date") is None
    assert _parse_jira_timestamp("") is None
    assert _parse_jira_timestamp(None) is None


# ---------- _find_bounce_reason ----------

def _comment(created: str, text: str, author_name: str | None = None) -> dict:
    """Build a minimal Jira-shaped comment dict for these tests."""
    body = {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }
    out: dict = {"created": created, "body": body}
    if author_name is not None:
        out["author"] = {"displayName": author_name}
    return out


def test_find_bounce_reason_returns_none_without_comments():
    assert _find_bounce_reason([], "2026-05-07T12:00:00.000Z", "Alice") is None


def test_find_bounce_reason_returns_none_when_transition_ts_unparseable():
    comments = [_comment("2026-05-07T12:00:00.000Z", "Broken")]
    assert _find_bounce_reason(comments, "garbage", "Alice") is None


def test_find_bounce_reason_picks_closest_comment_within_window():
    # Transition at 12:00. Closer comment (12:30) wins over farther one (15:00).
    comments = [
        _comment("2026-05-07T15:00:00.000Z", "way later"),
        _comment("2026-05-07T12:30:00.000Z", "right after"),
    ]
    reason = _find_bounce_reason(comments, "2026-05-07T12:00:00.000Z", None)
    assert reason == "right after"


def test_find_bounce_reason_excludes_comments_outside_six_hour_window():
    # 7 hours away, well outside the ±6h window.
    comments = [_comment("2026-05-07T19:00:01.000Z", "way later")]
    assert _find_bounce_reason(comments, "2026-05-07T12:00:00.000Z", None) is None


def test_find_bounce_reason_author_bonus_can_outrank_a_closer_comment():
    # Other author 1 minute away vs same author 25 minutes away — the
    # 30-minute author bonus tips the same-author comment ahead.
    comments = [
        _comment("2026-05-07T12:01:00.000Z", "other author closer", author_name="Bob"),
        _comment("2026-05-07T12:25:00.000Z", "same author farther", author_name="Alice"),
    ]
    reason = _find_bounce_reason(comments, "2026-05-07T12:00:00.000Z", "Alice")
    assert reason == "same author farther"


def test_find_bounce_reason_author_bonus_too_small_to_overcome_large_gap():
    # 30-minute author bonus shouldn't pull in a comment that's 5+ hours away
    # when there's a closer comment from someone else.
    comments = [
        _comment("2026-05-07T12:05:00.000Z", "very close, other author", author_name="Bob"),
        _comment("2026-05-07T17:30:00.000Z", "same author much later", author_name="Alice"),
    ]
    reason = _find_bounce_reason(comments, "2026-05-07T12:00:00.000Z", "Alice")
    assert reason == "very close, other author"


def test_find_bounce_reason_skips_empty_body():
    comments = [_comment("2026-05-07T12:01:00.000Z", "   ")]
    assert _find_bounce_reason(comments, "2026-05-07T12:00:00.000Z", None) is None


def test_find_bounce_reason_returns_short_body_untrimmed():
    body = "x" * 1500  # under the 2000-char cap
    comments = [_comment("2026-05-07T12:01:00.000Z", body)]
    reason = _find_bounce_reason(comments, "2026-05-07T12:00:00.000Z", None)
    assert reason == body


def test_find_bounce_reason_ignores_far_comment_without_state_entry_ts():
    # Without state_entry_ts, a comment weeks before the bounce stays invisible
    # — the fallback branch requires an anchor from the changelog.
    comments = [_comment("2026-06-24T13:22:00.000Z", "Spacing seems off", author_name="Megan")]
    assert (
        _find_bounce_reason(comments, "2026-07-09T09:19:00.000Z", "Tiffany") is None
    )


def test_find_bounce_reason_falls_back_to_reviewer_comment_in_state_window():
    # Bounce happens weeks after the ticket entered Ready for UAT. The
    # reviewer's earlier complaint should surface as the reason instead of
    # the dev's follow-up reply.
    comments = [
        _comment("2026-06-24T13:22:00.000Z", "Spacing seems off", author_name="Megan"),
        _comment("2026-06-24T13:34:00.000Z", "I just checked, will look", author_name="Ramtin"),
    ]
    reason = _find_bounce_reason(
        comments,
        "2026-07-09T09:19:00.000Z",
        "Tiffany",
        state_entry_ts="2026-06-16T11:29:00.000Z",
        dev_names=frozenset({"Ramtin"}),
    )
    assert reason == "Spacing seems off"


def test_find_bounce_reason_fallback_prefers_most_recent_when_all_non_dev():
    # No dev in play — plain recency decides.
    comments = [
        _comment("2026-06-20T09:00:00.000Z", "earlier reviewer note", author_name="Megan"),
        _comment("2026-06-24T13:22:00.000Z", "later reviewer note", author_name="Kyle"),
    ]
    reason = _find_bounce_reason(
        comments,
        "2026-07-09T09:19:00.000Z",
        "Tiffany",
        state_entry_ts="2026-06-16T11:29:00.000Z",
        dev_names=frozenset({"Ramtin"}),
    )
    assert reason == "later reviewer note"


def test_find_bounce_reason_fallback_falls_back_to_dev_when_only_dev_available():
    # A ticket where only the dev commented during the reviewed window still
    # gets a reason — the dev penalty demotes but doesn't erase.
    comments = [
        _comment("2026-06-20T09:00:00.000Z", "dev progress note", author_name="Ramtin"),
    ]
    reason = _find_bounce_reason(
        comments,
        "2026-07-09T09:19:00.000Z",
        "Tiffany",
        state_entry_ts="2026-06-16T11:29:00.000Z",
        dev_names=frozenset({"Ramtin"}),
    )
    assert reason == "dev progress note"


def test_find_bounce_reason_fallback_skips_comments_before_state_entry():
    # A comment posted before the ticket even entered the reviewed state
    # can't be the bounce reason for THIS run through that state.
    comments = [
        _comment("2026-05-01T12:00:00.000Z", "stale pre-review chatter", author_name="Megan"),
    ]
    reason = _find_bounce_reason(
        comments,
        "2026-07-09T09:19:00.000Z",
        "Tiffany",
        state_entry_ts="2026-06-16T11:29:00.000Z",
        dev_names=frozenset({"Ramtin"}),
    )
    assert reason is None


def test_find_bounce_reason_close_window_still_wins_over_fallback():
    # A close-window comment must beat a fallback candidate even when the
    # fallback would otherwise be picked.
    comments = [
        _comment("2026-06-24T13:22:00.000Z", "old reviewer note", author_name="Megan"),
        _comment("2026-07-09T09:20:00.000Z", "fresh transition note", author_name="Tiffany"),
    ]
    reason = _find_bounce_reason(
        comments,
        "2026-07-09T09:19:00.000Z",
        "Tiffany",
        state_entry_ts="2026-06-16T11:29:00.000Z",
        dev_names=frozenset({"Ramtin"}),
    )
    assert reason == "fresh transition note"


def test_find_bounce_reason_trims_long_body_at_sentence_boundary():
    # Long body with sentence boundaries in the back half — trim should land
    # on one of them, not slice mid-word like the old 1000-char hard cut did.
    prefix = "First sentence padding. " * 60  # ~1440 chars
    body = prefix + "This is a nice sentence boundary. Trailing filler filler filler." * 20
    comments = [_comment("2026-05-07T12:01:00.000Z", body)]
    reason = _find_bounce_reason(comments, "2026-05-07T12:00:00.000Z", None)
    assert reason is not None
    assert reason.endswith("…")
    # Must end on a sentence terminator — never a mid-word cut.
    stripped = reason[:-1].rstrip()
    assert stripped.endswith(".") or stripped.endswith("!") or stripped.endswith("?")


# ---------- _extract_bounce_history ----------

def _history(created: str, items: list[dict], author: str | None = None) -> dict:
    out: dict = {"created": created, "items": items}
    if author is not None:
        out["author"] = {"displayName": author}
    return out


def _status_item(from_status: str, to_status: str) -> dict:
    return {"field": "status", "fromString": from_status, "toString": to_status}


def test_extract_bounce_history_detects_qa_to_todo_bounce():
    histories = [
        _history("2026-05-07T10:00:00.000Z", [_status_item("To Do", "In Testing")], author="Alice"),
        _history("2026-05-07T12:00:00.000Z", [_status_item("In Testing", "To Do")], author="Bob"),
    ]
    comments = [_comment("2026-05-07T12:05:00.000Z", "Login broken on staging", author_name="Bob")]
    events = _extract_bounce_history(histories, comments)
    assert len(events) == 1
    bounce = events[0]
    assert bounce.from_status == "In Testing"
    assert bounce.to_status == "To Do"
    assert bounce.author == "Bob"
    assert bounce.reason == "Login broken on staging"


def test_extract_bounce_history_ignores_non_status_items():
    # Assignee changes shouldn't be mistaken for status transitions.
    histories = [
        _history("2026-05-07T10:00:00.000Z", [{"field": "assignee", "fromString": "x", "toString": "y"}]),
    ]
    assert _extract_bounce_history(histories, []) == []


def test_extract_bounce_history_skips_when_from_state_was_never_advanced():
    # In Progress → To Do isn't a QA bounce — the dev was never past their own work.
    histories = [
        _history("2026-05-07T12:00:00.000Z", [_status_item("In Progress", "To Do")]),
    ]
    assert _extract_bounce_history(histories, []) == []


def test_extract_bounce_history_requires_a_prior_advanced_state_globally():
    # If saw_advanced never flipped earlier, even an "In Testing → To Do" item
    # in isolation should still register because that single item flips the
    # flag (to_status is advanced) AND immediately checks the from_status.
    # But a "To Do → To Do" without any advanced state in history won't.
    histories = [
        _history("2026-05-07T12:00:00.000Z", [_status_item("To Do", "Backlog")]),
    ]
    assert _extract_bounce_history(histories, []) == []


def test_extract_bounce_history_records_multiple_bounces():
    histories = [
        _history("2026-05-07T10:00:00.000Z", [_status_item("To Do", "In Testing")]),
        _history("2026-05-07T11:00:00.000Z", [_status_item("In Testing", "To Do")]),
        _history("2026-05-07T13:00:00.000Z", [_status_item("To Do", "Ready for UAT")]),
        _history("2026-05-07T14:00:00.000Z", [_status_item("Ready for UAT", "In Progress")]),
    ]
    events = _extract_bounce_history(histories, [])
    assert len(events) == 2
    assert (events[0].from_status, events[0].to_status) == ("In Testing", "To Do")
    assert (events[1].from_status, events[1].to_status) == ("Ready for UAT", "In Progress")


def test_extract_bounce_history_handles_reverse_chronological_input():
    # Jira's API returns histories newest-first. The reviewed-state fallback
    # needs the state's entry timestamp, which is EARLIER in wall-clock time
    # than the bounce — so walking histories in the order Jira returned them
    # would miss it. Same shape as SK-2279 that surfaced the bug.
    histories = [
        _history("2026-07-09T09:19:00.000Z", [_status_item("Ready for UAT", "In Progress")], author="Tiffany"),
        _history("2026-06-16T11:29:00.000Z", [_status_item("In Testing", "Ready for UAT")], author="Ramtin"),
        _history("2026-06-15T11:25:00.000Z", [_status_item("Ready To Test", "In Testing")], author="Ramtin"),
    ]
    comments = [
        _comment("2026-06-24T13:22:00.000Z", "Spacing seems off", author_name="Megan"),
    ]
    events = _extract_bounce_history(histories, comments, dev_names=frozenset({"Ramtin"}))
    assert len(events) == 1
    assert events[0].reason == "Spacing seems off"


def test_extract_bounce_history_uses_reviewed_state_fallback_for_old_feedback():
    # SK-2279 shape: reviewer flagged an issue weeks before the eventual
    # bounce. Without the fallback, this rendered "No comment was posted
    # near this transition" even though the reviewer's comment is right there.
    histories = [
        _history("2026-06-16T11:30:00.000Z", [_status_item("In Progress", "Ready for UAT")], author="Alice"),
        _history("2026-07-09T09:19:00.000Z", [_status_item("Ready for UAT", "In Progress")], author="Tiffany"),
    ]
    comments = [
        _comment("2026-06-24T13:22:00.000Z", "Spacing seems off", author_name="Megan"),
        _comment("2026-06-24T13:34:00.000Z", "will look", author_name="Ramtin"),
    ]
    events = _extract_bounce_history(histories, comments, dev_names=frozenset({"Ramtin"}))
    assert len(events) == 1
    assert events[0].reason == "Spacing seems off"


def test_extract_bounce_history_leaves_reason_none_when_no_comment_matches():
    histories = [
        _history("2026-05-07T10:00:00.000Z", [_status_item("To Do", "In Testing")]),
        _history("2026-05-07T12:00:00.000Z", [_status_item("In Testing", "To Do")]),
    ]
    events = _extract_bounce_history(histories, [])
    assert len(events) == 1
    assert events[0].reason is None
