"""
Contract tests for `POST /jira/post-comment`.

The endpoint builds its response as an explicit allowlist of fields rather than
returning the client result verbatim, so anything post_comment learns to report
is dropped unless it is named there. That is how a split or partial post reaches
the UI looking like one clean comment — the default for every missing field is
"one comment, all of it landed". These pin the passthrough.
"""

from datetime import datetime
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


# --- Recording the posted version -------------------------------------------
#
# The comment landing on the ticket and the app recording it as the live
# version are two writes to two systems, and the second one used to fail in
# silence: `post_comment` catches everything around the mark so a database
# outage cannot cost the tester their posted plan. The cost was that the
# response still said `success: true` with nothing to distinguish it, the
# version badge kept reading "Not live in Jira", and posting again failed the
# same way every time. These pin the three outcomes apart.


def _post_with_plan(jira_result, *, mark=None, readback=None, plan_id=7):
    jira = MagicMock()
    jira.post_comment = AsyncMock(return_value=jira_result)
    repo = MagicMock()
    repo.mark_plan_posted_to_jira = AsyncMock(
        side_effect=mark if isinstance(mark, Exception) else None
    )
    repo.get_plan_with_cases = AsyncMock(
        side_effect=readback if isinstance(readback, Exception) else None,
        return_value=None if isinstance(readback, Exception) else readback,
    )
    with (
        patch("src.app.main.JiraClient", return_value=jira),
        patch("src.app.main.plan_repository", repo),
        patch("src.app.main.get_db", return_value=MagicMock()),
    ):
        return client.post(
            "/jira/post-comment",
            json={
                "issue_key": "TEST-1",
                "comment_text": "plan body",
                "plan_id": plan_id,
            },
        )


_ONE_COMMENT = {
    "id": "c1", "updated": False, "truncated": False,
    "parts": 1, "posted_parts": 1, "part_comment_ids": ["c1"],
    "stale_parts_left": 0, "part_error": None,
}


def _plan_posted_at(when):
    plan = MagicMock()
    plan.posted_at = when
    return (plan, [])


def test_a_lost_mark_is_reported_rather_than_read_as_success():
    """The case this endpoint kept shipping: the plan is on the ticket, the
    record of it is not, and the response used to be indistinguishable from a
    clean post."""
    res = _post_with_plan(_ONE_COMMENT, mark=RuntimeError("no reachable servers"))
    assert res.status_code == 200
    body = res.json()
    # The comment did land — this must not read as a failed post.
    assert body["success"] is True and body["comment_id"] == "c1"
    assert body["recorded"] is False
    assert body["record_error"]
    assert body["posted_at"] is None


def test_a_recorded_post_reports_the_mark_and_its_timestamp():
    res = _post_with_plan(
        _ONE_COMMENT, readback=_plan_posted_at(datetime(2026, 9, 22, 18, 59))
    )
    body = res.json()
    assert body["recorded"] is True
    assert body["posted_at"] == "2026-09-22T18:59:00"


def test_a_failed_read_back_does_not_unmake_a_mark_that_landed():
    """The mark is what makes the version live; the read-back only fetches its
    timestamp. Letting that second call flip `recorded` would report a lost
    write that never happened — and send the tester chasing a healthy post."""
    res = _post_with_plan(_ONE_COMMENT, readback=RuntimeError("read timed out"))
    body = res.json()
    assert body["recorded"] is True
    assert body["record_error"] is None
    assert body["posted_at"] is None


def test_a_post_with_no_stored_plan_has_nothing_to_record():
    """Null, not False: there was no plan row to mark, which the client already
    explains with its own notice. Reporting it as a lost write would cry wolf on
    every plan generated outside a run."""
    res = _post(_ONE_COMMENT)
    body = res.json()
    assert body["recorded"] is None
    assert body["record_error"] is None


def test_a_comment_with_no_id_cannot_be_recorded_and_says_so():
    res = _post_with_plan({**_ONE_COMMENT, "id": None})
    body = res.json()
    assert body["recorded"] is False
    assert body["record_error"]
