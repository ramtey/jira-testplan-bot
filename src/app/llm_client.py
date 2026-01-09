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
TEST PLAN REQUIREMENTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Generate a comprehensive test plan that ensures quality and prevents regressions.

**CRITICAL ANALYSIS FIRST:**
Before generating test cases, analyze the ticket description to identify:
1. **Categories/Groups**: Are there multiple categories, rule types, or scenarios mentioned? (e.g., "racism, fraud, FNF mentions, competitors" or "admin, user, guest roles")
2. **Behavior Patterns**: Are there different behaviors for different types? (e.g., "block chat for X, allow continue for Y")
3. **Specific Examples**: What concrete examples are provided in the description? (e.g., specific keywords, phrases, user types)

**If multiple categories exist, you MUST:**
- Create test cases covering representative examples from EACH category
- Use the specific examples provided in the ticket description
- Never create generic test cases like "test keyword blocking" - always use concrete examples like "test 'no section 8'"
- If different behaviors exist (e.g., hard block vs soft block), test examples from each behavior type

**1. Happy Path Tests (3-5 cases minimum)**
   - Cover the primary user flows and main functionality
   - **If multiple categories/scenarios exist:** Create AT LEAST one happy path test demonstrating normal operation (no restrictions triggered)
   - Write from the user's perspective (what they see/do)
   - Use clear, actionable steps with SPECIFIC EXAMPLES from the ticket
   - Each step must be concrete and verifiable
   - Example: "Enter 'What are your office hours?' in chat" for normal conversation

**2. Edge Cases & Error Scenarios (6-10 cases minimum for multi-category features)**
   **AUTOMATIC CATEGORY DETECTION:**
   - Scan the ticket description for lists, categories, or groups of items
   - If you find multiple categories (e.g., different keyword types, user roles, error conditions):
     * Create AT LEAST 1-2 test cases per category using specific examples from the ticket
     * Prioritize categories with the most detailed examples in the ticket
   - If behavior differs by category (e.g., "block for X, allow continue for Y"):
     * Create tests demonstrating EACH behavior type with specific examples

   **ALWAYS INCLUDE:**
   - Specific examples from the ticket description (not generic placeholders)
   - Complete steps AND expected results for every test case
   - Mixed/combined scenarios when applicable (e.g., "no section 8 families with kids")
   - Case sensitivity tests when relevant (e.g., "NO SECTION 8" vs "no section 8")
   - Boundary conditions and validation failures
   - Error messages must be specific and observable

**3. Regression Checklist (3-5 items)**
   - What existing functionality could this break?
   - Related features that must still work
   - Critical user flows that should be retested
   - Focus on high-risk areas mentioned in the ticket
   - IMPORTANT: Make these specific to the feature being tested, not generic examples

**4. Non-Functional Requirements (if applicable)**
   - Performance: Load times, response times, scalability concerns specific to this feature
   - Security: Authentication, authorization, data protection, XSS, SQL injection relevant to this feature
   - Accessibility: Keyboard navigation, screen readers, WCAG compliance for this feature
   - UX: Error messages, loading states, responsive design specific to this feature
   - Data validation: Input sanitization, data integrity for this feature
   - IMPORTANT: Only include items that are relevant to the feature described in the ticket

**5. Assumptions**
   - List any assumptions you're making about the implementation
   - Note dependencies on other systems or features
   - Clarify what you're assuming is in vs out of scope
   - IMPORTANT: Base assumptions on the actual ticket content, not generic scenarios

**6. Questions for PM/Developers**
   - What's unclear or ambiguous about the requirements?
   - What edge cases need clarification?
   - Are there technical constraints to consider?
   - IMPORTANT: Ask questions specific to this ticket, not generic questions

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Return ONLY valid JSON with this exact structure (no markdown, no code blocks):

{
  "happy_path": [
    {
      "title": "Descriptive test case name (user-focused, specific action)",
      "steps": [
        "Step 1: Specific, actionable step",
        "Step 2: Another concrete step",
        "Step 3: Final step with observable action"
      ],
      "expected": "Clear, measurable expected outcome (what the user sees/experiences)"
    }
  ],
  "edge_cases": [
    {
      "title": "Edge case or error scenario name",
      "steps": [
        "Step 1: Setup the edge case condition",
        "Step 2: Trigger the scenario"
      ],
      "expected": "Expected behavior (error message, validation, graceful failure)"
    }
  ],
  "regression_checklist": [
    "Specific existing feature to verify that's relevant to this ticket",
    "Another related feature to validate"
  ],
  "non_functional": [
    "Specific non-functional test relevant to this feature",
    "Another non-functional requirement specific to this ticket"
  ],
  "assumptions": [
    "Clear assumption about the implementation or requirements"
  ],
  "questions": [
    "Specific question that needs PM/Dev clarification"
  ]
}

**Quality Standards:**
- Write test steps from the user's perspective (not technical implementation)
- Be specific and actionable (avoid vague terms like "verify", "check", "test")
- Each test case should be independently executable
- Expected results must be observable and measurable
- Prioritize tests that catch real bugs (not just checklist items)

**CRITICAL JSON FORMATTING RULES:**
- ALL string arrays (regression_checklist, non_functional, assumptions, questions) MUST contain ONLY strings
- NEVER include objects in string arrays
- Example CORRECT: "non_functional": ["Response time under 500ms for keyword matching"]
- Example WRONG: "non_functional": [{"type": "performance", "test": "..."}]

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
        development_info: dict | None = None,
    ) -> TestPlan:
        """Generate test plan using Claude API."""
        prompt = self._build_prompt(ticket_key, summary, description, testing_context, development_info)

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
