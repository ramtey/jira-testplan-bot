"""
Contract tests for `POST /jira/post-comment`.

The endpoint builds its response as an explicit allowlist of fields rather than
returning the client result verbatim, so anything post_comment learns to report
is dropped unless it is named there. That is how a split or partial post reaches
the UI looking like one clean comment — the default for every missing field is
"one comment, all of it landed". These pin the passthrough.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from src.app.main import app

client = TestClient(app)


def _post(jira_result):
    jira = MagicMock()
    jira.post_comment = AsyncMock(return_value=jira_result)
    with patch("src.app.main.JiraClient", return_value=jira):
        return client.post(
            "/jira/post-comment",
            json={"issue_key": "TEST-1", "comment_text": "plan body"},
        )


def test_split_post_reports_how_many_comments_it_took():
    res = _post({
        "id": "c1", "updated": False, "truncated": False,
        "parts": 3, "posted_parts": 3,
        "part_comment_ids": ["c1", "c2", "c3"],
        "stale_parts_left": 0, "part_error": None,
    })
    assert res.status_code == 200
    body = res.json()
    assert body["parts"] == 3
    assert body["posted_parts"] == 3
    assert body["part_comment_ids"] == ["c1", "c2", "c3"]


def test_partial_post_is_not_reported_as_a_whole_one():
    """Two of three comments on the ticket is the case the UI most needs to
    tell apart from success, and the one an allowlist drops most quietly."""
    res = _post({
        "id": "c1", "updated": False, "truncated": False,
        "parts": 3, "posted_parts": 2,
        "part_comment_ids": ["c1", "c2"],
        "stale_parts_left": 0, "part_error": "Failed to reach Jira: boom",
    })
    body = res.json()
    assert body["posted_parts"] == 2 and body["parts"] == 3
    assert "boom" in body["part_error"]


def test_leftover_parts_that_could_not_be_deleted_are_reported():
    res = _post({
        "id": "c1", "updated": True, "truncated": False,
        "parts": 1, "posted_parts": 1, "part_comment_ids": ["c1"],
        "stale_parts_left": 2, "part_error": None,
    })
    assert res.json()["stale_parts_left"] == 2


def test_single_comment_post_reports_the_ordinary_shape():
    res = _post({"id": "c1", "updated": False, "truncated": False,
                 "parts": 1, "posted_parts": 1, "part_comment_ids": ["c1"],
                 "stale_parts_left": 0, "part_error": None})
    body = res.json()
    assert body["success"] is True
    assert body["parts"] == 1 and body["posted_parts"] == 1
    assert body["truncated"] is False
    # Never persisted, so there is no posted_at to report — the client uses
    # this to say "not tracked" instead of showing no Jira status at all.
    assert body["posted_at"] is None
