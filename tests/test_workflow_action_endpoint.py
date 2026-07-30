"""
End-to-end integration tests for `POST /issue/{key}/workflow/{action}`.

The endpoint orchestrates a lot: SK-project gate, walkthrough readiness gate,
image validation, attachment upload, transition lookup, a 3-tier assignee
resolution chain, the pass/fail comment posting, parent auto-transition, and
subtask cascade. The existing suite covers the helpers in isolation — these
tests drive the full handler through FastAPI's TestClient so we catch
regressions in how the pieces are wired.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from src.app.main import app

client = TestClient(app)


def _jira_stub(**overrides):
    """Build a JiraClient stub prewired for the happy-path workflow calls.

    Each test can override individual method behaviors via kwargs. Defaults:
    - list_transitions returns a single transition to "Ready for UAT"
    - get_my_account_id returns "bot-1"
    - get_prior_assignee_account_id returns ("dev-1", "Dev One")
    - get_top_pr_contributor_account_id returns (None, None) — prior wins
    - transition_issue / assign_issue succeed silently
    - post_qa_pass_comment / post_qa_fail_comment return a stub result
    - get_sibling_subtasks_info returns None so parent auto-transition no-ops
    """
    jira = MagicMock()
    jira.list_transitions = AsyncMock(
        return_value=[
            {"id": "31", "to": {"name": "Ready for UAT"}},
            {"id": "41", "to": {"name": "In Testing"}},
            {"id": "51", "to": {"name": "To Do"}},
            {"id": "61", "to": {"name": "In Progress"}},
        ]
    )
    jira.get_my_account_id = AsyncMock(return_value="bot-1")
    jira.get_prior_assignee_account_id = AsyncMock(return_value=("dev-1", "Dev One"))
    jira.get_top_pr_contributor_account_id = AsyncMock(return_value=(None, None))
    jira.transition_issue = AsyncMock(return_value=None)
    jira.assign_issue = AsyncMock(return_value=None)
    jira.post_qa_pass_comment = AsyncMock(return_value={"id": "c-1"})
    jira.post_qa_fail_comment = AsyncMock(return_value={"id": "c-2"})
    jira.get_sibling_subtasks_info = AsyncMock(return_value=None)
    jira.get_issue_status = AsyncMock(return_value="Ready to Test")
    jira.get_subtasks_of = AsyncMock(return_value=[])
    for name, value in overrides.items():
        setattr(jira, name, value)
    return jira


def test_rejects_non_sk_project():
    """Only the SK project is wired up right now — other keys must 400
    before any Jira calls."""
    with patch("src.app.workflow_routes.JiraClient") as jira_cls:
        jira_cls.return_value = _jira_stub()
        response = client.post(
            "/issue/AB-1/workflow/pull-to-testing",
            files={},
        )
    assert response.status_code == 400
    assert "SK project" in response.json()["detail"]


def test_rejects_unknown_action():
    """An action outside SK_WORKFLOW_ACTIONS must 400 with the name echoed
    back so the tester can spot a typo."""
    with patch("src.app.workflow_routes.JiraClient") as jira_cls:
        jira_cls.return_value = _jira_stub()
        response = client.post(
            "/issue/SK-1/workflow/warp-drive",
            files={},
        )
    assert response.status_code == 400
    assert "warp-drive" in response.json()["detail"]


def test_pull_to_testing_self_assigns_and_transitions():
    """pull-to-testing is the "I'll test this now" click. It always parks
    the ticket on the bot's own account (no prior-assignee lookup) and
    skips both the pass/fail comment path and the parent cascade."""
    jira = _jira_stub()
    with patch("src.app.workflow_routes.JiraClient", return_value=jira):
        response = client.post(
            "/issue/SK-42/workflow/pull-to-testing",
            files={},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["action"] == "pull-to-testing"
    assert body["target_status"] == "In Testing"
    assert body["assigned_to"] == "you"
    assert body["comment_posted"] is False
    assert body["parent_transitioned"] is False
    assert body["cascaded_subtasks"] == []

    # Actually transitioned + assigned to the bot itself, skipping the
    # prior-assignee lookup that fail/pass actions run.
    jira.transition_issue.assert_awaited_once_with("SK-42", "41")
    jira.assign_issue.assert_awaited_once_with("SK-42", "bot-1")
    jira.get_prior_assignee_account_id.assert_not_awaited()
    jira.post_qa_pass_comment.assert_not_awaited()
    jira.post_qa_fail_comment.assert_not_awaited()


def test_pass_to_uat_happy_path_with_loom_bypasses_walkthrough_gate():
    """Supplying a Loom URL in the payload counts as the walkthrough
    material, so the readiness gate stays silent and never hits the DB.
    The pass comment gets the Loom + summary + environments the tester
    typed in."""
    jira = _jira_stub()
    payload = (
        '{"loom_urls": ["https://loom.com/share/abc123"], '
        '"summary": "Manually verified in staging", '
        '"environments": ["staging"]}'
    )
    with patch("src.app.workflow_routes.JiraClient", return_value=jira):
        response = client.post(
            "/issue/SK-42/workflow/pass-to-uat",
            data={"payload": payload},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["assigned_to"] == "Dev One"
    assert body["target_status"] == "Ready for UAT"
    assert body["comment_posted"] is True

    jira.transition_issue.assert_awaited_once_with("SK-42", "31")
    jira.assign_issue.assert_awaited_once_with("SK-42", "dev-1")
    # Pass comment is called with loom_urls, summary, environments in the
    # positional slots defined by post_qa_pass_comment's signature.
    pass_args = jira.post_qa_pass_comment.await_args
    assert pass_args.args[0] == "SK-42"
    assert pass_args.args[1] == ["https://loom.com/share/abc123"]
    assert pass_args.args[2] == "Manually verified in staging"
    assert pass_args.args[3] == ["staging"]


def test_pass_to_uat_gates_high_complexity_ticket_without_walkthrough():
    """When the readiness helper says a ticket needs a walkthrough and
    the tester supplied nothing (no override, no Loom, no image), the
    endpoint must 409 with the walkthrough_required error code — the
    transition never runs."""
    jira = _jira_stub()
    with patch("src.app.workflow_routes.JiraClient", return_value=jira), \
            patch("src.app.workflow_routes.uat_readiness") as uat_mod:
        uat_mod.fetch_readiness = AsyncMock(
            return_value={"needs_walkthrough": True, "uat_complexity": "high"}
        )
        response = client.post(
            "/issue/SK-42/workflow/pass-to-uat",
            data={"payload": "{}"},
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error_code"] == "walkthrough_required"
    assert detail["uat_complexity"] == "high"
    # No Jira calls after the gate — ticket stays exactly where it was.
    jira.list_transitions.assert_not_awaited()
    jira.transition_issue.assert_not_awaited()


def test_pass_to_uat_override_flag_bypasses_readiness_gate():
    """`override_missing_walkthrough=true` is the "yes, ship it anyway"
    signal from the frontend. The DB readiness lookup must NOT run when
    the flag is set — the gate returns immediately."""
    jira = _jira_stub()
    with patch("src.app.workflow_routes.JiraClient", return_value=jira), \
            patch("src.app.workflow_routes.uat_readiness") as uat_mod:
        uat_mod.fetch_readiness = AsyncMock(
            return_value={"needs_walkthrough": True, "uat_complexity": "high"}
        )
        response = client.post(
            "/issue/SK-42/workflow/pass-to-uat",
            data={"payload": '{"override_missing_walkthrough": true}'},
        )

    assert response.status_code == 200
    uat_mod.fetch_readiness.assert_not_awaited()


def test_pass_to_uat_assignee_override_skips_auto_pick():
    """When the tester picks a specific person in the form, the endpoint
    must use that accountId verbatim and skip both the prior-assignee and
    PR-contributor lookups entirely."""
    jira = _jira_stub()
    payload = (
        '{"loom_urls": ["https://loom.com/share/x"], '
        '"assignee_override_set": true, '
        '"assignee_override_account_id": "picked-1", '
        '"assignee_override_display_name": "Chosen Person"}'
    )
    with patch("src.app.workflow_routes.JiraClient", return_value=jira):
        response = client.post(
            "/issue/SK-42/workflow/pass-to-uat",
            data={"payload": payload},
        )

    assert response.status_code == 200
    assert response.json()["assigned_to"] == "Chosen Person"
    jira.assign_issue.assert_awaited_once_with("SK-42", "picked-1")
    jira.get_prior_assignee_account_id.assert_not_awaited()
    jira.get_top_pr_contributor_account_id.assert_not_awaited()


def test_bot_safety_net_unassigns_when_prior_assignee_is_the_bot():
    """If the prior-assignee lookup resolves to the bot's own account
    (e.g., because pull-to-testing was the last action), the safety net
    must convert that to `unassigned` rather than parking the ticket back
    on the bot."""
    jira = _jira_stub(
        # get_prior_assignee_account_id is called with exclude_account_id
        # so it should already filter — but if a stale row leaks through
        # anyway, safety net catches it. Simulate that by forcing the same
        # accountId as the bot.
        get_prior_assignee_account_id=AsyncMock(return_value=("bot-1", "The Bot")),
        get_top_pr_contributor_account_id=AsyncMock(return_value=(None, None)),
    )
    payload = '{"loom_urls": ["https://loom.com/share/x"]}'
    with patch("src.app.workflow_routes.JiraClient", return_value=jira):
        response = client.post(
            "/issue/SK-42/workflow/pass-to-uat",
            data={"payload": payload},
        )

    assert response.status_code == 200
    assert response.json()["assigned_to"] == "unassigned"
    jira.assign_issue.assert_awaited_once_with("SK-42", None)


def test_fail_to_todo_posts_fail_comment_with_reason():
    """fail-to-todo bounces the ticket back to the developer with a
    required reason. The comment must be posted with that reason and
    the same assignee-resolution chain as pass-to-uat."""
    jira = _jira_stub()
    payload = '{"reason": "Search results are stale after filter change."}'
    with patch("src.app.workflow_routes.JiraClient", return_value=jira):
        response = client.post(
            "/issue/SK-42/workflow/fail-to-todo",
            data={"payload": payload},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["target_status"] == "To Do"
    assert body["assigned_to"] == "Dev One"
    assert body["comment_posted"] is True

    jira.transition_issue.assert_awaited_once_with("SK-42", "51")
    jira.assign_issue.assert_awaited_once_with("SK-42", "dev-1")
    fail_args = jira.post_qa_fail_comment.await_args
    assert fail_args.args[0] == "SK-42"
    assert fail_args.args[1] == "Search results are stale after filter change."


def test_pass_to_uat_swallows_comment_failure_but_still_returns_ok():
    """The transition + reassign already succeeded, so a comment failure
    must NOT roll back the workflow move. The endpoint returns 200 with
    comment_posted=False so the UI can surface the partial success."""
    jira = _jira_stub()
    jira.post_qa_pass_comment = AsyncMock(side_effect=RuntimeError("ADF 400"))
    payload = '{"loom_urls": ["https://loom.com/share/x"]}'
    with patch("src.app.workflow_routes.JiraClient", return_value=jira):
        response = client.post(
            "/issue/SK-42/workflow/pass-to-uat",
            data={"payload": payload},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["comment_posted"] is False
    # Ticket really did move — the comment blowup is isolated.
    jira.transition_issue.assert_awaited_once_with("SK-42", "31")


def test_missing_target_transition_returns_400_before_ticket_moves():
    """If the ticket's current status has no transition to the target,
    the endpoint 400s with the list of available transitions — nothing
    is transitioned."""
    jira = _jira_stub()
    jira.list_transitions = AsyncMock(
        return_value=[{"id": "99", "to": {"name": "Blocked"}}]
    )
    payload = '{"loom_urls": ["https://loom.com/share/x"]}'
    with patch("src.app.workflow_routes.JiraClient", return_value=jira):
        response = client.post(
            "/issue/SK-42/workflow/pass-to-uat",
            data={"payload": payload},
        )

    assert response.status_code == 400
    assert "Ready for UAT" in response.json()["detail"]
    assert "Blocked" in response.json()["detail"]
    jira.transition_issue.assert_not_awaited()


def test_invalid_payload_json_returns_400():
    """Malformed JSON in the `payload` form field is a client error, not
    a 500. It must abort before any Jira calls."""
    jira = _jira_stub()
    with patch("src.app.workflow_routes.JiraClient", return_value=jira):
        response = client.post(
            "/issue/SK-42/workflow/pass-to-uat",
            data={"payload": "{not valid json"},
        )

    assert response.status_code == 400
    assert "Invalid payload JSON" in response.json()["detail"]
    jira.list_transitions.assert_not_awaited()
