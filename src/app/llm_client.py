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

Generate a comprehensive, risk-based test plan that ensures quality and prevents regressions.

**PHASE 1: CRITICAL ANALYSIS**
Before generating test cases, thoroughly analyze the ticket to understand:

1. **Feature Scope & Complexity**:
   - What is the primary functionality being added/changed?
   - Is this frontend, backend, full-stack, or integration work?
   - What user roles/personas are affected?

2. **Categories & Variations**:
   - Are there multiple categories, rule types, or scenarios? (e.g., "admin/user/guest", "hard block/soft block")
   - What specific examples are provided? (e.g., keywords, phrases, data values)
   - Do different categories have different behaviors?

3. **Risk Assessment**:
   - What are the potential failure points? (authentication, data loss, payments, security)
   - What existing functionality could this break?
   - Are there security implications? (authentication, authorization, input validation, data exposure)

4. **Test Data Requirements**:
   - What types of accounts/users are needed? (admin, regular user, suspended account)
   - What input variations should be tested? (valid, invalid, edge cases, malicious)
   - Are there specific data states required? (empty database, full database, concurrent users)

**PHASE 2: GENERATE TEST CASES**

**1. Happy Path Tests (Quality over Quantity)**
   - Cover PRIMARY user flows that deliver business value
   - Focus on the most common, expected user journeys
   - Use **Given-When-Then** format for clarity:
     * Given: Initial state/preconditions (e.g., "Given user is logged in as admin")
     * When: Action taken (e.g., "When user clicks 'Export Data' button")
     * Then: Observable outcome (e.g., "Then CSV file downloads with 100 records")
   - Use SPECIFIC EXAMPLES from the ticket (not generic placeholders)
   - Mark priority: 🔴 Critical, 🟡 High, or 🟢 Medium

**2. Edge Cases & Error Scenarios (Risk-Based)**
   **Prioritize by risk and impact:**
   - **Security Tests** (if applicable):
     * Input validation (XSS: `<script>alert('test')</script>`, SQL injection: `' OR '1'='1`)
     * Authentication/authorization bypass attempts
     * Data exposure checks (ensure users can't access others' data)

   - **Boundary Value Analysis**:
     * Minimum/maximum values (e.g., 0 items, 1 item, max limit, max+1)
     * Empty/null/undefined inputs
     * Special characters and Unicode

   - **Category Coverage** (if multiple categories exist):
     * Test representative examples from EACH category mentioned in the ticket
     * Test behavioral differences (e.g., "hard block" shows error, "soft block" shows warning)

   - **Error Handling**:
     * Network failures, timeouts, API errors
     * Invalid user input with specific error messages
     * Concurrent operations and race conditions

   - **Integration Points**:
     * API contracts (correct request/response format)
     * Database transactions (data consistency, rollback on failure)
     * Third-party service failures (graceful degradation)

   **Format:** Use Given-When-Then, mark priority, include specific expected error messages

**3. Integration & Backend Tests (if applicable)**
   - API endpoint validation (request/response schemas, status codes)
   - Database operations (CRUD operations, constraints, indexes)
   - Service-to-service communication
   - Background jobs and async operations
   - Mark with 🔴 for critical data operations

**4. Regression Checklist**
   - Related features that MUST still work after this change
   - Critical user flows that could be impacted
   - High-traffic features that need extra validation
   - Make these SPECIFIC to the ticket (not generic like "test login")

**QUALITY STANDARDS:**
- ✅ Write from user's perspective, not implementation details
- ✅ Be specific and actionable (avoid vague "verify", "check", "test")
- ✅ Each test case is independently executable
- ✅ Expected results are observable and measurable
- ✅ Prioritize tests by risk: authentication > payments > data integrity > UI polish
- ✅ Include specific test data requirements (account types, input values)
- ✅ Use concrete examples from the ticket, never generic placeholders

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Return ONLY valid JSON with this exact structure (no markdown, no code blocks):

{
  "happy_path": [
    {
      "title": "User-focused test case name with specific action",
      "priority": "critical|high|medium",
      "steps": [
        "Given: Initial state and preconditions (e.g., 'Given user is logged in as admin')",
        "When: Specific action taken (e.g., 'When user clicks Export button')",
        "Then: Observable outcome (e.g., 'Then CSV file downloads with 100 records')"
      ],
      "expected": "Clear, measurable expected outcome",
      "test_data": "Specific data requirements (e.g., 'admin account with 100+ records')"
    }
  ],
  "edge_cases": [
    {
      "title": "Edge case or error scenario name (be specific)",
      "priority": "critical|high|medium",
      "category": "security|boundary|error_handling|integration|category_name",
      "steps": [
        "Given: Setup preconditions",
        "When: Trigger edge case action",
        "Then: Verify expected behavior"
      ],
      "expected": "Specific expected behavior (include exact error messages when applicable)",
      "test_data": "Required test data or inputs (e.g., 'invalid email: user@', SQL injection: \\' OR \\'1\\'=\\'1')"
    }
  ],
  "integration_tests": [
    {
      "title": "Backend/API/integration test name",
      "priority": "critical|high|medium",
      "steps": [
        "Given: System state",
        "When: API call or service interaction",
        "Then: Verify response/behavior"
      ],
      "expected": "Expected response, status code, or system behavior",
      "test_data": "API request payload or required data"
    }
  ],
  "regression_checklist": [
    "🔴 Critical: Specific existing feature that MUST work (e.g., 'User login with valid credentials')",
    "🟡 High: Important related feature (e.g., 'Password reset email delivery')",
    "🟢 Medium: Nice-to-have validation (e.g., 'Profile page loads correctly')"
  ]
}

**IMPORTANT NOTES:**

1. **Priority Levels**:
   - "critical": Authentication, payments, data loss, security vulnerabilities
   - "high": Core features, common user flows, data integrity
   - "medium": Edge cases, rare scenarios, UI polish

2. **Categories for edge_cases**:
   - "security": XSS, SQL injection, authentication bypass, authorization
   - "boundary": Min/max values, empty inputs, limits, special characters
   - "error_handling": Network failures, invalid input, timeouts
   - "integration": API contracts, database operations, third-party services
   - Or use specific category names from the ticket (e.g., "hard_block", "soft_block")

3. **Test Data Requirements**:
   - Be SPECIFIC: "admin account with billing permissions" not just "admin user"
   - Include exact invalid inputs: "email: user@" or "SQL: \\' OR \\'1\\'=\\'1"
   - Mention required system states: "database with 10,000 records"

4. **Integration Tests**:
   - Only include if ticket involves backend, API, or integration work
   - Focus on contracts, data flow, and service boundaries
   - If purely frontend UI work, you may omit this section or keep it empty: "integration_tests": []

**CRITICAL JSON FORMATTING RULES:**
- ALL arrays must contain either strings OR objects (never mixed)
- Use correct priority values: "critical", "high", "medium" (lowercase)
- Use Given-When-Then format in steps arrays
- If integration_tests not applicable, return empty array: []

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
