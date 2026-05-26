"""
Test the FastAPI endpoint with mocked Jira responses.

This allows you to test the full API without needing real Jira credentials.
"""

from unittest.mock import AsyncMock, MagicMock, patch

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
        "id": "10001",
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
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_jira_response
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
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
        print("✓ Issue with good description works!")
        print(f"  Labels: {data['labels']}")
        print(f"  Issue Type: {data['issue_type']}")
        print(f"  Extracted description:\n  {data['description'][:100]}...")


@pytest.mark.asyncio
async def test_issue_with_no_description():
    """Test fetching an issue with no description."""
    mock_jira_response = {
        "id": "10002",
        "key": "TEST-456",
        "fields": {
            "summary": "Fix login bug",
            "description": None,
            "labels": [],
            "issuetype": {"name": "Bug"},
        },
    }

    with patch("httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_jira_response
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        response = client.get("/issue/TEST-456")

        assert response.status_code == 200
        data = response.json()
        assert data["key"] == "TEST-456"
        assert data["description"] is None
        assert data["labels"] == []
        assert data["issue_type"] == "Bug"
        assert data["description_quality"]["has_description"] is False
        assert data["description_quality"]["gaps"] == ["Missing description"]
        print("✓ Issue with no description works!")
        print(f"  Issue Type: {data['issue_type']}")
        print(f"  Gaps: {data['description_quality']['gaps']}")


@pytest.mark.asyncio
async def test_issue_with_weak_description():
    """Test fetching an issue with a very short description."""
    mock_jira_response = {
        "id": "10003",
        "key": "TEST-789",
        "fields": {
            "summary": "Update UI",
            "description": "Make it blue",
            "labels": ["ui", "design"],
            "issuetype": {"name": "Task"},
        },
    }

    with patch("httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_jira_response
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        response = client.get("/issue/TEST-789")

        assert response.status_code == 200
        data = response.json()
        assert data["key"] == "TEST-789"
        assert data["description"] == "Make it blue"
        assert data["labels"] == ["ui", "design"]
        assert data["issue_type"] == "Task"
        assert data["description_quality"]["has_description"] is True
        assert data["description_quality"]["char_count"] < 50
        assert "Missing acceptance criteria" in data["description_quality"]["gaps"]
        print("✓ Issue with weak description works!")
        print(f"  Labels: {data['labels']}")
        print(f"  Issue Type: {data['issue_type']}")
        print(f"  Gaps: {data['description_quality']['gaps']}")


@pytest.mark.asyncio
async def test_issue_not_found():
    """Test 404 error when issue doesn't exist."""
    with patch("httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        response = client.get("/issue/NOTFOUND-999")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()
        print("✓ 404 error handling works!")


@pytest.mark.asyncio
async def test_auth_error():
    """Test 401 error for authentication failure."""
    with patch("httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.json.return_value = {}
        mock_response.text = ""
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        response = client.get("/issue/TEST-123")

        assert response.status_code == 401
        assert "authentication" in response.json()["detail"].lower()
        print("✓ 401 error handling works!")


@pytest.mark.asyncio
async def test_permission_error():
    """Test 403 error for permission denied."""
    with patch("httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        response = client.get("/issue/TEST-123")

        assert response.status_code == 403
        assert "forbidden" in response.json()["detail"].lower()
        print("✓ 403 error handling works!")


@pytest.mark.asyncio
async def test_connection_error():
    """Test 502 error when Jira is unreachable."""
    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=httpx.ConnectError("Connection failed")
        )

        response = client.get("/issue/TEST-123")

        assert response.status_code == 502
        assert "failed to reach jira" in response.json()["detail"].lower()
        print("✓ Connection error handling works!")


# ── Multi-ticket cross-project mode ────────────────────────────────────────

from src.app.models import TestPlan as _TestPlan  # noqa: E402


def _multi_payload(ticket_key: str, repo: str, filename: str, patch: str) -> dict:
    return {
        "ticket_key": ticket_key,
        "summary": f"Summary for {ticket_key}",
        "description": "Some description",
        "issue_type": "Story",
        "testing_context": {},
        "development_info": {
            "pull_requests": [
                {
                    "repository": repo,
                    "files_changed": [
                        {
                            "filename": filename,
                            "status": "added",
                            "patch": patch,
                        }
                    ],
                }
            ],
            "commits": [],
            "branches": [],
        },
        "comments": [],
    }


def _stub_llm(captured: dict, *, cross_project_summary: dict | None = None):
    class _Stub:
        async def generate_multi_ticket_test_plan(self, *, tickets, images, cross_project=None):
            captured["cross_project"] = cross_project
            captured["tickets"] = tickets
            return _TestPlan(
                happy_path=[{"title": "Smoke", "priority": "high", "steps": ["x"], "expected": "ok"}],
                edge_cases=[],
                regression_checklist=[],
                integration_tests=[
                    {
                        "title": "Cross-repo: quote endpoint",
                        "priority": "high",
                        "steps": ["x"],
                        "expected": "ok",
                        "cross_project": True,
                        "seam": {
                            "kind": "http_route",
                            "identifier": "GET /quote",
                            "producer_repo": "agent-calculator",
                            "consumer_repo": "compliance",
                            "verified": True,
                        },
                    }
                ],
                cross_project_summary=cross_project_summary,
            )

    return _Stub()


def test_multi_ticket_cross_project_returns_summary():
    """Two tickets across different repos: response now succeeds (no more 422)
    and includes the seam catalog. The LLM stub also receives the cross_project
    kwarg so the prompt-side wiring is exercised."""
    captured: dict = {}
    payload = {
        "tickets": [
            _multi_payload(
                "PROJ-1",
                "agent-calculator",
                "src/main.py",
                '@@ -0,0 +1,1 @@\n+@app.get("/quote")',
            ),
            _multi_payload(
                "PROJ-2",
                "compliance",
                "src/api.ts",
                '@@ -0,0 +1,1 @@\n+await fetch("/quote")',
            ),
        ]
    }

    with patch("src.app.main.get_llm_client", return_value=_stub_llm(captured)):
        response = client.post("/generate-test-plan/multi", json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert "cross_project_summary" in body
    summary = body["cross_project_summary"]
    assert any(
        s["identifier"] == "GET /quote"
        and s["producer"]["repo"] == "agent-calculator"
        and s["consumer"]["repo"] == "compliance"
        for s in summary["verified_seams"]
    )
    # Stub received the kwarg
    assert captured["cross_project"] is not None
    assert any(
        s["identifier"] == "GET /quote"
        for s in captured["cross_project"]["verified_seams"]
    )


def test_multi_ticket_single_repo_omits_summary():
    """Same repo for both tickets → mode stays single_repo, no
    cross_project_summary in the response, kwarg is None."""
    captured: dict = {}
    payload = {
        "tickets": [
            _multi_payload(
                "PROJ-1",
                "files-ui",
                "src/a.py",
                "@@ -0,0 +1,1 @@\n+x = 1",
            ),
            _multi_payload(
                "PROJ-2",
                "files-ui",
                "src/b.py",
                "@@ -0,0 +1,1 @@\n+y = 2",
            ),
        ]
    }

    with patch("src.app.main.get_llm_client", return_value=_stub_llm(captured)):
        response = client.post("/generate-test-plan/multi", json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert "cross_project_summary" not in body
    assert captured["cross_project"] is None


def test_multi_ticket_cross_project_rejects_non_testable_issue_type():
    """Cross-project mode doesn't change the existing Epic/Spike rejection."""
    payload = {
        "tickets": [
            _multi_payload(
                "PROJ-1",
                "agent-calculator",
                "src/main.py",
                "@@ -0,0 +1,1 @@\n+x = 1",
            ),
            {
                **_multi_payload(
                    "PROJ-2",
                    "compliance",
                    "src/api.ts",
                    "@@ -0,0 +1,1 @@\n+y = 2",
                ),
                "issue_type": "Epic",
            },
        ]
    }

    response = client.post("/generate-test-plan/multi", json=payload)
    assert response.status_code == 400
    assert "Epic" in response.json()["detail"]


if __name__ == "__main__":
    print("Running manual API tests with mocked Jira responses...\n")
    print("=" * 60)

    # Run synchronous tests
    test_health_endpoint()

    # For async tests, you'll need to run with pytest
    print("\n" + "=" * 60)
    print("To run async tests, use: pytest test_api_mock.py -v")
    print("Or install pytest: uv add --dev pytest pytest-asyncio")
