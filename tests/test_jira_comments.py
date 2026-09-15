"""
Test Jira comment posting and replacement functionality.

This tests the smart comment management feature that updates existing
test plan comments instead of creating duplicates.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.jira_client import (
    JIRA_COMMENT_MAX_BYTES,
    JIRA_COMMENT_MAX_PARTS,
    JiraClient,
    JiraConnectionError,
    TEST_PLAN_MARKER,
    _adf_size,
    _fit_to_jira_comment_limit,
    _split_marked_text_into_parts,
    _wrap_body_in_expand,
    markdown_to_adf,
)


@pytest.mark.asyncio
async def test_post_comment_creates_new_when_none_exists():
    """Test that posting creates a new comment when none exists."""
    jira = JiraClient()

    # Mock get_comments to return empty list (no existing comments)
    with patch.object(jira, 'get_comments', return_value=[]):
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "id": "12345",
                "body": {"type": "doc", "version": 1, "content": []}
            }
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            result = await jira.post_comment("TEST-123", "Test plan content")

            assert result["id"] == "12345"
            assert result["updated"] is False


@pytest.mark.asyncio
async def test_post_comment_updates_existing():
    """Test that posting updates existing test plan comment."""
    jira = JiraClient()

    # Mock get_comments to return existing test plan comment
    existing_comment = {
        "id": "67890",
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "🤖 Generated Test Plan\n\nOld content"
                        }
                    ]
                }
            ]
        }
    }

    with patch.object(jira, 'get_comments', return_value=[existing_comment]):
        with patch.object(jira, 'update_comment') as mock_update:
            mock_update.return_value = {
                "id": "67890",
                "body": {"type": "doc", "version": 1, "content": []}
            }

            result = await jira.post_comment("TEST-123", "New test plan content")

            # Verify update was called instead of create
            mock_update.assert_called_once()
            assert result["updated"] is True
            assert result["id"] == "67890"


@pytest.mark.asyncio
async def test_post_comment_creates_new_when_marker_not_found():
    """Test that posting creates new comment when marker is not found in existing comments."""
    jira = JiraClient()

    # Mock get_comments to return comments without marker
    existing_comment = {
        "id": "11111",
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "Regular comment without marker"
                        }
                    ]
                }
            ]
        }
    }

    with patch.object(jira, 'get_comments', return_value=[existing_comment]):
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "id": "22222",
                "body": {"type": "doc", "version": 1, "content": []}
            }
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            result = await jira.post_comment("TEST-123", "Test plan content")

            # Should create new comment since marker wasn't found
            assert result["id"] == "22222"
            assert result["updated"] is False


@pytest.mark.asyncio
async def test_post_comment_includes_marker():
    """Test that posted comment includes the marker for future identification."""
    jira = JiraClient()

    with patch.object(jira, 'get_comments', return_value=[]):
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {"id": "12345"}
            mock_post = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__.return_value.post = mock_post

            await jira.post_comment("TEST-123", "Test plan content")

            # Verify the posted payload includes the marker (in content[0]) and
            # the comment body (wrapped in an expand block at content[1]).
            import json as _json

            call_args = mock_post.call_args
            payload = call_args.kwargs['json']
            marker_text = payload['body']['content'][0]['content'][0]['text']
            full_body = _json.dumps(payload['body'])

            assert "🤖 Generated Test Plan" in marker_text
            assert "Test plan content" in full_body


@pytest.mark.asyncio
async def test_post_comment_fallback_on_error():
    """Test that posting falls back to creating new comment if checking fails."""
    jira = JiraClient()

    # Mock get_comments to raise an exception
    with patch.object(jira, 'get_comments', side_effect=Exception("API error")):
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "id": "12345",
                "body": {"type": "doc", "version": 1, "content": []}
            }
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            # Should still succeed by creating new comment
            result = await jira.post_comment("TEST-123", "Test plan content")

            assert result["id"] == "12345"
            assert result["updated"] is False


def test_wrap_body_in_expand_collapses_each_test_case():
    """Each test case should become its own `nestedExpand` so reviewers see
    titles after the first click and steps only after clicking a case.
    Section banners stay visible; the `──` dividers are dropped."""
    markdown = (
        f"{TEST_PLAN_MARKER}\n\n"
        "✅ HAPPY PATH TEST CASES\n\n"
        "**1. Logs in with valid creds 🔴 CRITICAL**\n\n"
        "Steps:\n"
        "1. Open /login\n"
        "2. Submit valid creds\n\n"
        "Expected Result: dashboard loads\n\n"
        "────────────────────────────────────────────\n\n"
        "**2. Resets password 🟡 HIGH**\n\n"
        "Steps:\n"
        "1. Click 'forgot password'\n\n"
        "Expected Result: reset email sent\n\n"
        "🔄 REGRESSION CHECKLIST\n\n"
        "  • verify SSO still works\n"
    )
    wrapped = _wrap_body_in_expand(markdown_to_adf(markdown))

    outer = wrapped["content"][1]
    assert outer["type"] == "expand"
    inner = outer["content"]
    nested = [n for n in inner if n.get("type") == "nestedExpand"]
    assert [n["attrs"]["title"] for n in nested] == [
        "1. Logs in with valid creds 🔴 CRITICAL",
        "2. Resets password 🟡 HIGH",
        "🔄 REGRESSION CHECKLIST",
    ]
    # Steps live inside the per-case nestedExpand, not at the outer level.
    flat_outer_text = " ".join(
        _txt
        for n in inner if n.get("type") != "nestedExpand"
        for _txt in [
            "".join(c.get("text", "") for c in n.get("content", []) if c.get("type") == "text")
        ]
    )
    assert "Open /login" not in flat_outer_text
    assert "Submit valid creds" not in flat_outer_text
    # The regression checklist bullets are inside its nestedExpand, not at the outer level.
    assert "verify SSO" not in flat_outer_text
    regression = next(n for n in nested if n["attrs"]["title"].startswith("🔄"))
    regression_text = " ".join(
        "".join(c.get("text", "") for c in child.get("content", []) if c.get("type") == "text")
        for child in regression["content"]
    )
    assert "verify SSO" in regression_text
    # And no leftover ── divider paragraphs anywhere in the body.
    import json as _json
    body = _json.dumps(wrapped)
    assert "────" not in body


if __name__ == "__main__":
    print("Running Jira comment tests...")
    print("=" * 60)
    print("\nTo run these tests, use: pytest tests/test_jira_comments.py -v")
    print("Or install pytest: uv add --dev pytest pytest-asyncio")


def test_fit_to_jira_comment_limit_reports_untruncated():
    """A plan that already fits comes back unchanged and flagged untruncated."""
    text = f"{TEST_PLAN_MARKER}\n\nA short plan."
    fitted, truncated = _fit_to_jira_comment_limit(text)
    assert fitted == text
    assert truncated is False


def test_fit_to_jira_comment_limit_reports_truncated():
    """An oversized plan is cut, and the caller is told so.

    The notice inside the text is for whoever reads the Jira comment; the flag
    is for the poster, who otherwise gets a plain success and no hint that the
    comment is missing most of the plan.
    """
    text = f"{TEST_PLAN_MARKER}\n\n" + ("- A long test case step line.\n" * 4000)
    fitted, truncated = _fit_to_jira_comment_limit(text)
    assert truncated is True
    assert len(fitted) < len(text)
    assert "Plan truncated" in fitted


@pytest.mark.asyncio
async def test_post_comment_flags_truncation_on_new_comment():
    """post_comment surfaces truncation when it creates a comment."""
    jira = JiraClient()
    oversized = "- A long test case step line.\n" * 4000

    with patch.object(jira, 'get_comments', return_value=[]):
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {"id": "12345"}
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            result = await jira.post_comment("TEST-123", oversized)

            assert result["updated"] is False
            assert result["truncated"] is True


@pytest.mark.asyncio
async def test_post_comment_flags_truncation_on_update():
    """The update-in-place path reports truncation too, not just the create path."""
    jira = JiraClient()
    oversized = "- A long test case step line.\n" * 4000
    existing_comment = {
        "id": "67890",
        "body": {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": TEST_PLAN_MARKER}],
                }
            ],
        },
    }

    with patch.object(jira, 'get_comments', return_value=[existing_comment]):
        with patch.object(jira, 'update_comment', return_value={"id": "67890"}):
            result = await jira.post_comment("TEST-123", oversized)

    assert result["updated"] is True
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_post_comment_reports_no_truncation_for_short_plan():
    """A plan that fits is not flagged, so the UI can report a clean success."""
    jira = JiraClient()

    with patch.object(jira, 'get_comments', return_value=[]):
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {"id": "12345"}
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            result = await jira.post_comment("TEST-123", "Test plan content")

    assert result["truncated"] is False


# --- Splitting an oversized plan across several comments ------------------
#
# Jira rejects a comment over its size limit outright, so an oversized plan used
# to be cut to fit and the tester silently lost the tail. These cover the split
# that replaced that: the whole plan reaching the ticket across consecutive
# comments, and the cases where something still has to be reported as lost.


def _case_block(n: int) -> str:
    return (
        f"**{n}. Verify the agent can submit the listing form 🔴 CRITICAL**\n\n"
        "Preconditions: Agent is logged in with an active subscription.\n\n"
        "Steps:\n"
        "1. Navigate to the Listings tab from the main dashboard\n"
        "2. Tap the draft listing named \"123 Main St\"\n"
        "3. Fill in the required address, price, and square footage fields\n"
        "4. Tap Submit at the bottom of the form\n\n"
        "Expected Result: The listing is submitted and the status becomes Pending Review.\n\n"
        "Test Data: address=123 Main St, price=450000, sqft=2100\n\n"
        "────────────────────────────────────────────\n\n"
    )


def _oversized_plan(cases: int = 29) -> str:
    return (
        "✅ HAPPY PATH TEST CASES\n\n"
        + "".join(_case_block(i + 1) for i in range(cases // 2))
        + "🔗 INTEGRATION & BACKEND TESTS\n\n"
        + "".join(_case_block(i + 1) for i in range(cases - cases // 2))
    )


def test_plan_that_fits_stays_one_comment():
    """The common case is untouched: one part, byte-identical to the input."""
    text = f"{TEST_PLAN_MARKER}\n\nShort plan.\n\nSteps:\n1. Do it\n"
    parts, truncated = _split_marked_text_into_parts(text)
    assert truncated is False
    assert parts == [text]


def test_oversized_plan_is_split_not_cut():
    """A plan too big for one comment keeps every case, spread over parts.

    This is the whole point of the split: the old behaviour dropped roughly a
    third of a 29-case plan and told the tester to go read it in the app.
    """
    plan = _oversized_plan(29)
    text = f"{TEST_PLAN_MARKER}\n\n{plan}"
    assert _adf_size(text) > JIRA_COMMENT_MAX_BYTES, "fixture is not actually oversized"

    parts, truncated = _split_marked_text_into_parts(text)

    assert truncated is False
    assert len(parts) > 1
    assert all(_adf_size(part) <= JIRA_COMMENT_MAX_BYTES for part in parts)
    # Every case title survives, and none is duplicated across parts.
    joined = "".join(parts)
    for n in range(1, 15):
        assert joined.count(f"**{n}. Verify the agent") == plan.count(f"**{n}. Verify the agent")


def test_split_parts_are_numbered_and_carry_the_marker():
    """Each part must open with the marker, or the update-in-place path stops
    recognising its own comments and starts piling up duplicates."""
    parts, _ = _split_marked_text_into_parts(f"{TEST_PLAN_MARKER}\n\n{_oversized_plan(29)}")
    total = len(parts)
    for i, part in enumerate(parts):
        assert part.startswith(f"{TEST_PLAN_MARKER} (part {i + 1} of {total})")


def test_split_never_breaks_a_test_case_in_half():
    """A part that began mid-case would render as headless steps, because the
    collapse grouping keys off the `**N. Title**` line."""
    parts, _ = _split_marked_text_into_parts(f"{TEST_PLAN_MARKER}\n\n{_oversized_plan(29)}")
    for part in parts[1:]:
        body = part.split("\n\n", 1)[1]
        first_real = next(line for line in body.splitlines() if line.strip())
        assert first_real.startswith("**") or first_real.startswith(("✅", "🔍", "🔗", "🔄")), first_real


def test_continuation_part_repeats_the_open_section_banner():
    parts, _ = _split_marked_text_into_parts(f"{TEST_PLAN_MARKER}\n\n{_oversized_plan(29)}")
    continued = [p for p in parts[1:] if "(continued)" in p.split("\n\n")[1]]
    assert continued, "a mid-section split should repeat the banner"


def test_plan_past_the_part_cap_is_truncated_and_says_so():
    """The cap still exists — the failure just moved several times further out,
    and it is still reported rather than left inside the comment."""
    huge = "- A long test case step line.\n" * 4000
    parts, truncated = _split_marked_text_into_parts(f"{TEST_PLAN_MARKER}\n\n{huge}")
    assert truncated is True
    assert len(parts) == JIRA_COMMENT_MAX_PARTS
    assert "Plan truncated" in parts[-1]
    assert all(_adf_size(part) <= JIRA_COMMENT_MAX_BYTES for part in parts)


@pytest.mark.asyncio
async def test_post_comment_creates_one_comment_per_part():
    jira = JiraClient()
    created: list[str] = []

    async def fake_create(issue_key, text):
        created.append(text)
        return {"id": f"c{len(created)}"}

    with patch.object(jira, 'get_comments', return_value=[]):
        with patch.object(jira, '_create_comment', side_effect=fake_create):
            result = await jira.post_comment("TEST-123", _oversized_plan(29))

    assert len(created) > 1
    assert result["parts"] == len(created)
    assert result["posted_parts"] == len(created)
    assert result["truncated"] is False
    assert result["id"] == "c1"
    assert result["part_comment_ids"] == [f"c{i + 1}" for i in range(len(created))]


def _marker_comment(comment_id: str) -> dict:
    return {
        "id": comment_id,
        "body": {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": TEST_PLAN_MARKER}]}
            ],
        },
    }


@pytest.mark.asyncio
async def test_regenerating_reuses_existing_part_comments_in_order():
    """Reposting has to land on the comments the last post made, or the ticket
    accumulates a new copy of the plan every time it is regenerated."""
    jira = JiraClient()
    updated: list[str] = []

    async def fake_update(issue_key, comment_id, text):
        updated.append(comment_id)
        return {"id": comment_id}

    existing = [_marker_comment("10"), _marker_comment("11")]
    with patch.object(jira, 'get_comments', return_value=existing):
        with patch.object(jira, 'update_comment', side_effect=fake_update):
            with patch.object(jira, '_create_comment', side_effect=AssertionError("should reuse")):
                result = await jira.post_comment("TEST-123", _oversized_plan(29))

    assert updated == ["10", "11"]
    assert result["updated"] is True
    assert result["id"] == "10"


@pytest.mark.asyncio
async def test_shorter_replan_deletes_the_leftover_parts():
    """A two-part plan replaced by a one-part plan must not leave part 2 on the
    ticket — a tester reading it would run cases the current plan dropped."""
    jira = JiraClient()
    deleted: list[str] = []

    async def fake_delete(issue_key, comment_id):
        deleted.append(comment_id)

    existing = [_marker_comment("10"), _marker_comment("11"), _marker_comment("12")]
    with patch.object(jira, 'get_comments', return_value=existing):
        with patch.object(jira, 'update_comment', return_value={"id": "10"}):
            with patch.object(jira, 'delete_comment', side_effect=fake_delete):
                result = await jira.post_comment("TEST-123", "A short plan that fits.")

    assert deleted == ["11", "12"]
    assert result["parts"] == 1
    assert result["stale_parts_left"] == 0


@pytest.mark.asyncio
async def test_undeletable_leftover_parts_are_reported():
    jira = JiraClient()
    existing = [_marker_comment("10"), _marker_comment("11")]
    with patch.object(jira, 'get_comments', return_value=existing):
        with patch.object(jira, 'update_comment', return_value={"id": "10"}):
            with patch.object(jira, 'delete_comment', side_effect=RuntimeError("nope")):
                result = await jira.post_comment("TEST-123", "A short plan that fits.")

    assert result["stale_parts_left"] == 1


@pytest.mark.asyncio
async def test_failure_partway_through_reports_what_landed():
    """Part 1 on the ticket and part 2 rejected is not a failed post, and it is
    not a clean one either. Reporting it as either is a lie about the ticket."""
    jira = JiraClient()
    calls = {"n": 0}

    async def flaky_create(issue_key, text):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"id": "c1"}
        raise JiraConnectionError("Failed to reach Jira: boom")

    with patch.object(jira, 'get_comments', return_value=[]):
        with patch.object(jira, '_create_comment', side_effect=flaky_create):
            result = await jira.post_comment("TEST-123", _oversized_plan(29))

    assert result["posted_parts"] == 1
    assert result["parts"] > 1
    assert "boom" in result["part_error"]


@pytest.mark.asyncio
async def test_failure_on_the_first_part_still_raises():
    """Nothing reached the ticket, so this is a plain failed post."""
    jira = JiraClient()

    with patch.object(jira, 'get_comments', return_value=[]):
        with patch.object(jira, '_create_comment', side_effect=JiraConnectionError("down")):
            with pytest.raises(JiraConnectionError):
                await jira.post_comment("TEST-123", _oversized_plan(29))
