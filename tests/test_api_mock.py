"""
Test the FastAPI endpoint with mocked Jira responses.

This allows you to test the full API without needing real Jira credentials.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from src.app.main import app

client = TestClient(app)


def test_health_endpoint():
    """Test the health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    print("✓ Health endpoint works!")


@pytest.mark.asyncio
async def test_issue_with_good_description():
    """Test fetching an issue with a well-structured ADF description."""
    mock_jira_response = {
        "key": "TEST-123",
        "fields": {
            "summary": "Add password reset functionality",
            "labels": ["security", "user-management"],
            "issuetype": {"name": "Story"},
            "description": {
                "version": 1,
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Users should be able to reset their password via email.",
                            }
                        ],
                    },
                    {
                        "type": "heading",
                        "attrs": {"level": 2},
                        "content": [{"type": "text", "text": "Acceptance Criteria"}],
                    },
                    {
                        "type": "bulletList",
                        "content": [
                            {
                                "type": "listItem",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": "Given a user clicks 'Forgot Password', when they enter their email, then they receive a reset link",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                ],
            },
        },
    }

    with patch("httpx.AsyncClient") as mock_client:
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_jira_response
        mock_client.return_value.__aenter__.return_value.get.return_value = (
            mock_response
        )

        response = client.get("/issue/TEST-123")

        assert response.status_code == 200
        data = response.json()
        assert data["key"] == "TEST-123"
        assert data["summary"] == "Add password reset functionality"
        assert data["labels"] == ["security", "user-management"]
        assert data["issue_type"] == "Story"
        assert "Users should be able to reset their password" in data["description"]
        assert "Acceptance Criteria" in data["description"]
        assert data["description_quality"]["has_description"] is True
        assert len(data["description_quality"]["warnings"]) == 0
        print("✓ Issue with good description works!")
        print(f"  Labels: {data['labels']}")
        print(f"  Issue Type: {data['issue_type']}")
        print(f"  Extracted description:\n  {data['description'][:100]}...")


@pytest.mark.asyncio
async def test_issue_with_no_description():
    """Test fetching an issue with no description."""
    mock_jira_response = {
        "key": "TEST-456",
        "fields": {
            "summary": "Fix login bug",
            "description": None,
            "labels": [],
            "issuetype": {"name": "Bug"},
        },
    }

    with patch("httpx.AsyncClient") as mock_client:
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_jira_response
        mock_client.return_value.__aenter__.return_value.get.return_value = (
            mock_response
        )

        response = client.get("/issue/TEST-456")

        assert response.status_code == 200
        data = response.json()
        assert data["key"] == "TEST-456"
        assert data["description"] is None
        assert data["labels"] == []
        assert data["issue_type"] == "Bug"
        assert data["description_quality"]["has_description"] is False
        assert data["description_quality"]["is_weak"] is True
        assert len(data["description_quality"]["warnings"]) > 0
        assert "No description provided" in data["description_quality"]["warnings"][0]
        print("✓ Issue with no description works!")
        print(f"  Issue Type: {data['issue_type']}")
        print(f"  Warnings: {data['description_quality']['warnings']}")


@pytest.mark.asyncio
async def test_issue_with_weak_description():
    """Test fetching an issue with a very short description."""
    mock_jira_response = {
        "key": "TEST-789",
        "fields": {
            "summary": "Update UI",
            "description": "Make it blue",
            "labels": ["ui", "design"],
            "issuetype": {"name": "Task"},
        },
    }

    with patch("httpx.AsyncClient") as mock_client:
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_jira_response
        mock_client.return_value.__aenter__.return_value.get.return_value = (
            mock_response
        )

        response = client.get("/issue/TEST-789")

        assert response.status_code == 200
        data = response.json()
        assert data["key"] == "TEST-789"
        assert data["description"] == "Make it blue"
        assert data["labels"] == ["ui", "design"]
        assert data["issue_type"] == "Task"
        assert data["description_quality"]["has_description"] is True
        assert data["description_quality"]["is_weak"] is True
        assert data["description_quality"]["char_count"] < 50
        assert len(data["description_quality"]["warnings"]) > 0
        print("✓ Issue with weak description works!")
        print(f"  Labels: {data['labels']}")
        print(f"  Issue Type: {data['issue_type']}")
        print(f"  Warnings: {data['description_quality']['warnings']}")


@pytest.mark.asyncio
async def test_issue_not_found():
    """Test 404 error when issue doesn't exist."""
    with patch("httpx.AsyncClient") as mock_client:
        mock_response = AsyncMock()
        mock_response.status_code = 404
        mock_client.return_value.__aenter__.return_value.get.return_value = (
            mock_response
        )

        response = client.get("/issue/NOTFOUND-999")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()
        print("✓ 404 error handling works!")


@pytest.mark.asyncio
async def test_auth_error():
    """Test 401 error for authentication failure."""
    with patch("httpx.AsyncClient") as mock_client:
        mock_response = AsyncMock()
        mock_response.status_code = 401
        mock_client.return_value.__aenter__.return_value.get.return_value = (
            mock_response
        )

        response = client.get("/issue/TEST-123")

        assert response.status_code == 401
        assert "authentication" in response.json()["detail"].lower()
        print("✓ 401 error handling works!")


@pytest.mark.asyncio
async def test_permission_error():
    """Test 403 error for permission denied."""
    with patch("httpx.AsyncClient") as mock_client:
        mock_response = AsyncMock()
        mock_response.status_code = 403
        mock_client.return_value.__aenter__.return_value.get.return_value = (
            mock_response
        )

        response = client.get("/issue/TEST-123")

        assert response.status_code == 403
        assert "forbidden" in response.json()["detail"].lower()
        print("✓ 403 error handling works!")


@pytest.mark.asyncio
async def test_connection_error():
    """Test 502 error when Jira is unreachable."""
    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get.side_effect = (
            httpx.ConnectError("Connection failed")
        )

        response = client.get("/issue/TEST-123")

        assert response.status_code == 502
        assert "failed to reach jira" in response.json()["detail"].lower()
        print("✓ Connection error handling works!")


if __name__ == "__main__":
    print("Running manual API tests with mocked Jira responses...\n")
    print("=" * 60)

    # Run synchronous tests
    test_health_endpoint()

    # For async tests, you'll need to run with pytest
    print("\n" + "=" * 60)
    print("To run async tests, use: pytest test_api_mock.py -v")
    print("Or install pytest: uv add --dev pytest pytest-asyncio")
