"""
Test Jira comment posting and replacement functionality.

This tests the smart comment management feature that updates existing
test plan comments instead of creating duplicates.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.app.jira_client import JiraClient


@pytest.mark.asyncio
async def test_post_comment_creates_new_when_none_exists():
    """Test that posting creates a new comment when none exists."""
    jira = JiraClient()

    # Mock get_comments to return empty list (no existing comments)
    with patch.object(jira, 'get_comments', return_value=[]):
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = AsyncMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "id": "12345",
                "body": {"type": "doc", "version": 1, "content": []}
            }
            mock_client.return_value.__aenter__.return_value.post.return_value = mock_response

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
            mock_response = AsyncMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "id": "22222",
                "body": {"type": "doc", "version": 1, "content": []}
            }
            mock_client.return_value.__aenter__.return_value.post.return_value = mock_response

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
            mock_response = AsyncMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {"id": "12345"}
            mock_client.return_value.__aenter__.return_value.post.return_value = mock_response

            await jira.post_comment("TEST-123", "Test plan content")

            # Verify the posted payload includes the marker
            call_args = mock_client.return_value.__aenter__.return_value.post.call_args
            payload = call_args.kwargs['json']
            content_text = payload['body']['content'][0]['content'][0]['text']

            assert "🤖 Generated Test Plan" in content_text
            assert "Test plan content" in content_text


@pytest.mark.asyncio
async def test_post_comment_fallback_on_error():
    """Test that posting falls back to creating new comment if checking fails."""
    jira = JiraClient()

    # Mock get_comments to raise an exception
    with patch.object(jira, 'get_comments', side_effect=Exception("API error")):
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = AsyncMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "id": "12345",
                "body": {"type": "doc", "version": 1, "content": []}
            }
            mock_client.return_value.__aenter__.return_value.post.return_value = mock_response

            # Should still succeed by creating new comment
            result = await jira.post_comment("TEST-123", "Test plan content")

            assert result["id"] == "12345"
            assert result["updated"] is False


if __name__ == "__main__":
    print("Running Jira comment tests...")
    print("=" * 60)
    print("\nTo run these tests, use: pytest tests/test_jira_comments.py -v")
    print("Or install pytest: uv add --dev pytest pytest-asyncio")
