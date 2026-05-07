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


def test_find_bounce_reason_truncates_long_body():
    long_text = "x" * 1500
    comments = [_comment("2026-05-07T12:01:00.000Z", long_text)]
    reason = _find_bounce_reason(comments, "2026-05-07T12:00:00.000Z", None)
    assert reason is not None
    assert reason.endswith("...")
    assert len(reason) == 1003  # 1000 chars + "..."


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


def test_extract_bounce_history_leaves_reason_none_when_no_comment_matches():
    histories = [
        _history("2026-05-07T10:00:00.000Z", [_status_item("To Do", "In Testing")]),
        _history("2026-05-07T12:00:00.000Z", [_status_item("In Testing", "To Do")]),
    ]
    events = _extract_bounce_history(histories, [])
    assert len(events) == 1
    assert events[0].reason is None
