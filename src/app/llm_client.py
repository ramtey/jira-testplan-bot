"""
LLM client abstraction layer.

Supports multiple LLM providers with a unified interface:
- Ollama (local, free)
- Claude API (Anthropic, paid)

Switch providers by changing LLM_PROVIDER in .env
"""

import json
from abc import ABC, abstractmethod

import httpx

from .config import settings
from .models import TestPlan


class LLMError(Exception):
    """Base exception for LLM-related errors."""

    pass


class LLMClient(ABC):
    """Abstract base class for LLM clients."""

    @abstractmethod
    async def generate_test_plan(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        testing_context: dict,
        development_info: dict | None = None,
    ) -> TestPlan:
        """Generate a structured test plan from ticket data and context."""
        pass

    def _build_prompt(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        testing_context: dict,
        development_info: dict | None = None,
    ) -> str:
        """Build the prompt for test plan generation (shared across providers)."""
        prompt = f"""You are an expert QA engineer with 10+ years of experience creating comprehensive test plans. Your role is to generate thorough, actionable test cases that catch bugs before they reach production.

**Your Task:** Create a detailed test plan for the following Jira ticket.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TICKET INFORMATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**Ticket:** {ticket_key}
**Summary:** {summary}

**Description:**
{description if description else "No description provided"}
"""

        # Add development information if available
        if development_info:
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "DEVELOPMENT ACTIVITY\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "\nThe following development work has been completed for this ticket:\n"

            # Add pull request information
            pull_requests = development_info.get("pull_requests", [])
            if pull_requests:
                prompt += f"\n**Pull Requests ({len(pull_requests)}):**\n"
                for pr in pull_requests:
                    prompt += f"- {pr.get('title', 'Untitled PR')} (Status: {pr.get('status', 'UNKNOWN')})\n"
                    if pr.get('source_branch'):
                        prompt += f"  Branch: {pr.get('source_branch')}\n"

            # Add commit information
            commits = development_info.get("commits", [])
            if commits:
                prompt += f"\n**Commits ({len(commits)}):**\n"
                # Show first 10 commit messages to avoid overwhelming the prompt
                for commit in commits[:10]:
                    commit_msg = commit.get('message', 'No message').split('\n')[0]  # First line only
                    author = commit.get('author', 'Unknown')
                    prompt += f"- {commit_msg} (by {author})\n"
                if len(commits) > 10:
                    prompt += f"... and {len(commits) - 10} more commits\n"

            # Add branch information
            branches = development_info.get("branches", [])
            if branches:
                prompt += f"\n**Branches:**\n"
                for branch in branches:
                    prompt += f"- {branch}\n"

            prompt += "\n**Use this development context to:**\n"
            prompt += "- Infer what functionality was implemented from commit messages and PR titles\n"
            prompt += "- Identify potential risk areas based on what code was changed\n"
            prompt += "- Generate more specific test cases based on the actual implementation\n"
            prompt += "- Focus testing on the areas that were modified\n"

        # Add user-provided context if available
        if testing_context.get("acceptanceCriteria"):
            prompt += f"\n**Acceptance Criteria:**\n{testing_context['acceptanceCriteria']}\n"

        if testing_context.get("specialInstructions"):
            prompt += f"\n**Special Testing Instructions:**\n{testing_context['specialInstructions']}\n"

        prompt += """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GENERATE TEST PLAN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Create a clear, actionable test plan organized by feature/component. Extract requirements from any format provided and focus on functional testing from a user perspective.

**ADJUST SCOPE BASED ON COMPLEXITY:**
Analyze the ticket complexity and adjust test coverage accordingly:

- **Simple Bug Fixes** (UI glitches, text changes, minor visual issues):
  - 2-3 happy path tests (verify fix works in main scenarios)
  - 2-3 edge cases (boundary conditions, theme switching)
  - 3-5 regression items (related features still work)
  - Skip integration tests unless system interaction is involved

- **Medium Features** (single component changes, API endpoints, form additions):
  - 3-5 happy path tests (cover main user flows)
  - 4-6 edge cases (error handling, validation, boundaries)
  - 5-8 regression items (impacted areas)
  - Include integration tests if APIs or multiple components involved

- **Complex Features** (multi-system changes, new workflows, security features):
  - 5-8 happy path tests (comprehensive flow coverage)
  - 6-10 edge cases (security, concurrency, data integrity)
  - 8-12 regression items (extensive impact analysis)
  - Include integration tests for system interactions

**ORGANIZE TESTS BY FEATURE/COMPONENT:**
Group related test cases logically by the feature or component they test.

**INCLUDE THESE TEST TYPES:**

1. **Positive Scenarios (Happy Path)**
   - Test the main user flow with valid inputs
   - Cover the most common expected user actions
   - Use specific examples from the ticket (not generic placeholders)

2. **Negative Scenarios (Error Handling)**
   - Test with invalid inputs, missing data, unauthorized access
   - Verify proper error messages are shown
   - Include specific examples: invalid email formats, wrong passwords, etc.

3. **Edge Cases (Boundary Conditions)**
   - Test minimum/maximum values (0 items, 1 item, max limit, max+1)
   - Test empty states (empty lists, no data, blank fields)
   - Test special characters and unusual inputs

4. **Integration Scenarios**
   - Test when multiple features interact together
   - Test data flow between components
   - Only include if multiple systems/features are involved

5. **Reset/Clear Functionality**
   - Test any reset, clear, or undo operations
   - Verify data is properly cleared/restored

**FORMAT EACH TEST AS: ACTION → EXPECTED RESULT**
Each test should include:
- Clear action steps (what the user does)
- Expected result (what should happen)
- Specific test data when needed

**GUIDELINES:**
- Write from the user's perspective (avoid technical implementation details)
- Be specific and actionable (use concrete examples)
- Each test should be independently executable
- Include data validation testing when applicable
- Identify ambiguities or missing information when present
- Prioritize tests: critical > high > medium

**PRIORITY LEVELS:**
- "critical": Authentication, payments, data loss, security issues
- "high": Core functionality, common user flows, data integrity
- "medium": Edge cases, rare scenarios, minor issues

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Return ONLY valid JSON (no markdown, no code blocks):

{
  "happy_path": [
    {
      "title": "Clear test name describing the action",
      "priority": "critical|high|medium",
      "steps": [
        "First action step",
        "Second action step",
        "Third action step"
      ],
      "expected": "What should happen (observable result)",
      "test_data": "Specific data needed (e.g., 'admin user with email test@example.com')"
    }
  ],
  "edge_cases": [
    {
      "title": "Clear test name for edge case or error scenario",
      "priority": "critical|high|medium",
      "category": "security|boundary|error_handling|integration",
      "steps": [
        "Setup step",
        "Action that triggers edge case",
        "Verification step"
      ],
      "expected": "Expected behavior (include error messages if applicable)",
      "test_data": "Specific edge case data (e.g., 'empty string', 'max+1 value: 101')"
    }
  ],
  "integration_tests": [
    {
      "title": "Test name for feature interaction or API test",
      "priority": "critical|high|medium",
      "steps": [
        "Setup multiple components",
        "Action involving multiple features",
        "Verify interaction"
      ],
      "expected": "Expected interaction result",
      "test_data": "Data needed for integration test"
    }
  ],
  "regression_checklist": [
    "🔴 Critical feature that must still work (be specific)",
    "🟡 Important related feature",
    "🟢 Additional validation item"
  ]
}

**RULES:**
- Steps array should contain plain action descriptions without numbering (numbering will be added during display)
- Priority values: "critical", "high", or "medium" (lowercase)
- Categories: "security", "boundary", "error_handling", "integration"
- If integration_tests not needed, return empty array: []
- Use specific examples from the ticket, never generic placeholders
- All test_data should be concrete and specific

Generate the test plan now:"""
        return prompt


class OllamaClient(LLMClient):
    """Ollama LLM client (local, free)."""

    def __init__(self):
        self.base_url = settings.ollama_base_url.rstrip("/")
        self.model = settings.llm_model

    async def generate_test_plan(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        testing_context: dict,
        development_info: dict | None = None,
    ) -> TestPlan:
        """Generate test plan using Ollama."""
        prompt = self._build_prompt(ticket_key, summary, description, testing_context, development_info)

        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "format": "json",
                        "stream": False,
                        "options": {"temperature": 0.7},
                    },
                )
                response.raise_for_status()

                data = response.json()
                response_text = data.get("response", "")

                # Parse JSON response
                try:
                    test_plan_data = json.loads(response_text)
                except json.JSONDecodeError as e:
                    raise LLMError(
                        f"Failed to parse JSON response from Ollama: {e}"
                    ) from e

                return TestPlan(
                    happy_path=test_plan_data.get("happy_path", []),
                    edge_cases=test_plan_data.get("edge_cases", []),
                    regression_checklist=test_plan_data.get("regression_checklist", []),
                    integration_tests=test_plan_data.get("integration_tests", []),
                )

        except httpx.ConnectError as e:
            raise LLMError(
                f"Failed to connect to Ollama at {self.base_url}. Is Ollama running? Error: {e}"
            ) from e
        except httpx.TimeoutException as e:
            raise LLMError(
                f"Ollama request timed out after 300s. Try a smaller model or increase timeout. Error: {e}"
            ) from e
        except httpx.HTTPStatusError as e:
            raise LLMError(
                f"Ollama returned error status {e.response.status_code}: {e.response.text}"
            ) from e


class ClaudeClient(LLMClient):
    """Claude API client (Anthropic, paid)."""

    def __init__(self):
        if not settings.anthropic_api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY not set. Please add it to your .env file to use Claude."
            )

        self.api_key = settings.anthropic_api_key
        self.model = settings.llm_model or "claude-3-5-sonnet-20241022"

    async def generate_test_plan(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        testing_context: dict,
        development_info: dict | None = None,
    ) -> TestPlan:
        """Generate test plan using Claude API."""
        prompt = self._build_prompt(ticket_key, summary, description, testing_context, development_info)

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "anthropic-version": "2023-06-01",
                        "x-api-key": self.api_key,
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 8192,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.7,
                    },
                )
                response.raise_for_status()

                data = response.json()
                response_text = data["content"][0]["text"]

                # Claude may wrap JSON in markdown code blocks, so strip those
                response_text = response_text.strip()
                if response_text.startswith("```"):
                    # Remove markdown code blocks
                    response_text = response_text.split("```")[1]
                    if response_text.startswith("json"):
                        response_text = response_text[4:]
                    response_text = response_text.strip()

                # Parse JSON response
                try:
                    test_plan_data = json.loads(response_text)
                except json.JSONDecodeError as e:
                    # Log the problematic response for debugging
                    print(f"DEBUG: Failed to parse JSON. Error: {e}")
                    print(f"DEBUG: Response text (first 1000 chars): {response_text[:1000]}")
                    print(f"DEBUG: Response text (around error): {response_text[max(0, e.pos-100):e.pos+100]}")
                    raise LLMError(
                        f"Failed to parse JSON response from Claude: {e}. "
                        f"This usually happens with unescaped quotes in test case descriptions. "
                        f"Response snippet: {response_text[max(0, e.pos-50):e.pos+50]}"
                    ) from e

                return TestPlan(
                    happy_path=test_plan_data.get("happy_path", []),
                    edge_cases=test_plan_data.get("edge_cases", []),
                    regression_checklist=test_plan_data.get("regression_checklist", []),
                    integration_tests=test_plan_data.get("integration_tests", []),
                )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise LLMError(
                    "Claude API authentication failed. Check your ANTHROPIC_API_KEY."
                ) from e
            raise LLMError(
                f"Claude API returned error status {e.response.status_code}: {e.response.text}"
            ) from e
        except httpx.TimeoutException as e:
            raise LLMError(f"Claude API request timed out: {e}") from e


def get_llm_client() -> LLMClient:
    """
    Factory function to get the appropriate LLM client based on configuration.

    Returns:
        LLMClient: The configured LLM client (Ollama or Claude)

    Raises:
        LLMError: If provider is unsupported or configuration is invalid
    """
    provider = settings.llm_provider.lower()

    if provider == "ollama":
        return OllamaClient()
    elif provider == "claude":
        return ClaudeClient()
    else:
        raise LLMError(
            f"Unsupported LLM provider: {provider}. Use 'ollama' or 'claude'."
        )
