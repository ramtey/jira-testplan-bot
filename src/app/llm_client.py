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
    ) -> TestPlan:
        """Generate a structured test plan from ticket data and context."""
        pass

    def _build_prompt(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        testing_context: dict,
    ) -> str:
        """Build the prompt for test plan generation (shared across providers)."""
        prompt = f"""You are a QA expert helping generate comprehensive test plans for software development tickets.

**Jira Ticket: {ticket_key}**
**Summary:** {summary}

**Description:**
{description if description else "No description provided"}

**Additional Testing Context:**
"""

        # Add user-provided context if available
        if testing_context.get("acceptanceCriteria"):
            prompt += f"\n**Acceptance Criteria:**\n{testing_context['acceptanceCriteria']}\n"

        if testing_context.get("testDataNotes"):
            prompt += f"\n**Test Data Notes:**\n{testing_context['testDataNotes']}\n"

        if testing_context.get("environments"):
            prompt += f"\n**Environments:**\n{testing_context['environments']}\n"

        if testing_context.get("rolesPermissions"):
            prompt += f"\n**Roles/Permissions:**\n{testing_context['rolesPermissions']}\n"

        if testing_context.get("outOfScope"):
            prompt += f"\n**Out of Scope:**\n{testing_context['outOfScope']}\n"

        if testing_context.get("riskAreas"):
            prompt += f"\n**Risk Areas:**\n{testing_context['riskAreas']}\n"

        prompt += """

Generate a comprehensive test plan in JSON format with the following structure:

{
  "happy_path": [
    {
      "title": "Test case title",
      "steps": ["Step 1", "Step 2", "Step 3"],
      "expected": "Expected result"
    }
  ],
  "edge_cases": [
    {
      "title": "Edge case title",
      "steps": ["Step 1", "Step 2"],
      "expected": "Expected result"
    }
  ],
  "regression_checklist": [
    "Item to verify hasn't broken",
    "Another regression concern"
  ],
  "non_functional": [
    "Performance consideration",
    "Security consideration"
  ],
  "assumptions": [
    "Assumption 1",
    "Assumption 2"
  ],
  "questions": [
    "Question for PM/Dev 1",
    "Question for PM/Dev 2"
  ]
}

**Guidelines:**
- Include 2-4 happy path test cases covering the main functionality
- Include 2-3 edge cases (boundary conditions, error handling, unexpected inputs)
- List 3-5 regression items to verify existing functionality isn't broken
- Add relevant non-functional concerns (performance, security, accessibility, UX)
- Note any assumptions you're making
- List questions that need clarification from PM or developers

Respond ONLY with valid JSON matching the schema above. No markdown formatting, no code blocks, just raw JSON.
"""
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
    ) -> TestPlan:
        """Generate test plan using Ollama."""
        prompt = self._build_prompt(ticket_key, summary, description, testing_context)

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
                    non_functional=test_plan_data.get("non_functional", []),
                    assumptions=test_plan_data.get("assumptions", []),
                    questions=test_plan_data.get("questions", []),
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
    ) -> TestPlan:
        """Generate test plan using Claude API."""
        prompt = self._build_prompt(ticket_key, summary, description, testing_context)

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "anthropic-version": "2023-06-01",
                        "x-api-key": self.api_key,
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 4096,
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
                    raise LLMError(
                        f"Failed to parse JSON response from Claude: {e}"
                    ) from e

                return TestPlan(
                    happy_path=test_plan_data.get("happy_path", []),
                    edge_cases=test_plan_data.get("edge_cases", []),
                    regression_checklist=test_plan_data.get("regression_checklist", []),
                    non_functional=test_plan_data.get("non_functional", []),
                    assumptions=test_plan_data.get("assumptions", []),
                    questions=test_plan_data.get("questions", []),
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
