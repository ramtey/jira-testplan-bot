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

    def __init__(self, message: str, error_type: str = "service_unavailable") -> None:
        """
        Initialize LLMError.

        Args:
            message: Error message
            error_type: Type of error - "invalid", "expired", "rate_limited", "service_unavailable"
        """
        super().__init__(message)
        self.error_type = error_type


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
        images: list[tuple[str, str]] | None = None,
    ) -> TestPlan:
        """Generate a structured test plan from ticket data and context.

        Args:
            images: List of (base64_data, media_type) tuples for image analysis
        """
        pass

    def _build_prompt(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        testing_context: dict,
        development_info: dict | None = None,
        has_images: bool = False,
    ) -> str:
        """Build the prompt for test plan generation (shared across providers)."""
        prompt = f"""You are an expert QA engineer with 10+ years of experience creating comprehensive test plans. Your role is to generate thorough, actionable test cases that catch bugs before they reach production.

**Your Task:** Create a detailed test plan for the following Jira ticket{" (screenshots/mockups attached)" if has_images else ""}.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TICKET INFORMATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**Ticket:** {ticket_key}
**Summary:** {summary}

**Description:**
{description if description else "No description provided"}
"""

        if has_images:
            prompt += "\n**Note:** Screenshots or mockups are attached. Use them to understand the UI requirements and generate specific visual test cases.\n"

        # Add repository context if available (Phase 4: Repository Documentation)
        if development_info and development_info.get("repository_context"):
            repo_context = development_info["repository_context"]
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "PROJECT DOCUMENTATION\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

            readme = repo_context.get("readme_content")
            if readme:
                # Include README (truncate if very long)
                readme_preview = readme[:2000] + "..." if len(readme) > 2000 else readme
                prompt += f"\n**README.md:**\n{readme_preview}\n"

            test_examples = repo_context.get("test_examples")
            if test_examples:
                prompt += f"\n**Test File Examples Found:**\n"
                for test_file in test_examples[:5]:
                    prompt += f"- {test_file}\n"
                prompt += "\nUse these test patterns and project documentation to generate specific test cases that match this project's structure and conventions.\n"

        # Add Figma design context if available (Phase 5)
        if development_info and development_info.get("figma_context"):
            figma_context = development_info["figma_context"]
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "DESIGN SPECIFICATIONS (FIGMA)\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += f"\n**Design File:** {figma_context.get('file_name')}\n"

            # Add frames/screens (limit to 30)
            frames = figma_context.get("frames", [])
            if frames:
                prompt += f"\n**Screens/Frames ({len(frames)}):**\n"
                for frame in frames[:30]:
                    frame_name = frame.get("name") if isinstance(frame, dict) else frame.name
                    frame_type = frame.get("type", "FRAME") if isinstance(frame, dict) else frame.type
                    prompt += f"- {frame_name} ({frame_type})\n"

            # Add components (limit to 20)
            components = figma_context.get("components", [])
            if components:
                prompt += f"\n**UI Components ({len(components)}):**\n"
                for comp in components[:20]:
                    comp_name = comp.get("name") if isinstance(comp, dict) else comp.name
                    comp_desc = comp.get("description") if isinstance(comp, dict) else comp.description
                    comp_info = f"- {comp_name}"
                    if comp_desc:
                        comp_info += f": {comp_desc}"
                    prompt += comp_info + "\n"

            prompt += "\n**Use this design context to:**\n"
            prompt += "- Reference actual screen names and UI component names from Figma\n"
            prompt += "- Generate UI-specific test cases using exact component names\n"
            prompt += "- Create visual validation tests for each screen/frame\n"
            prompt += "- Ensure test steps match the design specifications\n"

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
                    prompt += f"- **{pr.get('title', 'Untitled PR')}** (Status: {pr.get('status', 'UNKNOWN')})\n"
                    if pr.get('source_branch'):
                        prompt += f"  Branch: {pr.get('source_branch')}\n"

                    # Add GitHub PR description if available (Phase 3a)
                    gh_desc = pr.get('github_description')
                    if gh_desc:
                        # Truncate long descriptions
                        desc_preview = gh_desc[:300] + "..." if len(gh_desc) > 300 else gh_desc
                        prompt += f"  PR Description: {desc_preview}\n"

                    # Add code changes summary if available (Phase 3a)
                    files_changed = pr.get('files_changed')
                    if files_changed:
                        total_additions = pr.get('total_additions', 0)
                        total_deletions = pr.get('total_deletions', 0)
                        prompt += f"  📊 Code Changes: {len(files_changed)} files modified (+{total_additions}/-{total_deletions})\n"

                        # Show modified files (limit to 15 most significant)
                        prompt += "  📁 Modified Files:\n"
                        sorted_files = sorted(files_changed, key=lambda f: f.get('changes', 0), reverse=True)
                        for file_change in sorted_files[:15]:
                            filename = file_change.get('filename', 'unknown')
                            status = file_change.get('status', 'modified')
                            additions = file_change.get('additions', 0)
                            deletions = file_change.get('deletions', 0)

                            status_icon = {
                                "added": "✨",
                                "modified": "📝",
                                "removed": "🗑️",
                                "renamed": "📛",
                            }.get(status, "📄")

                            prompt += f"     {status_icon} {filename} (+{additions}/-{deletions})\n"

                        if len(files_changed) > 15:
                            prompt += f"     ... and {len(files_changed) - 15} more files\n"

                        prompt += "\n"

                    # Add PR comments if available (Phase 3b)
                    comments = pr.get('comments')
                    if comments:
                        prompt += f"  💬 PR Discussion ({len(comments)} comments):\n"
                        # Show most recent/relevant comments (limit to 10)
                        for comment in comments[:10]:
                            author = comment.get('author', 'unknown')
                            body = comment.get('body', '')
                            comment_type = comment.get('comment_type', 'conversation')

                            # Truncate long comments
                            body_preview = body[:200] + "..." if len(body) > 200 else body

                            # Format differently for review comments (they have file context)
                            icon = "📝" if comment_type == "review_comment" else "💬"
                            prompt += f"     {icon} @{author}: {body_preview}\n"

                        if len(comments) > 10:
                            prompt += f"     ... and {len(comments) - 10} more comments\n"

                        prompt += "\n"

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
            prompt += "- Understand the project structure and architecture from the README documentation\n"
            prompt += "- Use project-specific terminology, UI component names, and navigation patterns from the documentation\n"
            prompt += "- Generate test steps with actual screen names, button labels, and menu items (not generic placeholders)\n"
            prompt += "- Infer what functionality was implemented from commit messages and PR titles\n"
            prompt += "- Analyze the modified files to identify which components/modules were changed\n"
            prompt += "- Extract edge cases and gotchas mentioned in PR comments and code review discussions\n"
            prompt += "- Identify concerns, bugs, or scenarios discussed by developers during code review\n"
            prompt += "- Identify potential risk areas based on the type and scope of code changes\n"
            prompt += "- Generate specific test cases targeting the modified files and their dependencies\n"
            prompt += "- Focus testing on high-risk areas (authentication, payments, data handling, etc.)\n"
            prompt += "- Consider edge cases related to the specific code changes made\n"

        # Add user-provided context if available
        if testing_context.get("acceptanceCriteria"):
            prompt += f"\n**Acceptance Criteria:**\n{testing_context['acceptanceCriteria']}\n"

        if testing_context.get("specialInstructions"):
            prompt += f"\n**Special Testing Instructions:**\n{testing_context['specialInstructions']}\n"

        prompt += """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ CRITICAL: STAY GROUNDED IN ACTUAL REQUIREMENTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**YOU MUST ONLY TEST WHAT IS EXPLICITLY MENTIONED:**
- ONLY create test cases for features/fields/UI elements explicitly described in the ticket, PR changes, or context
- DO NOT invent test cases for features that "should" exist based on your domain knowledge
- DO NOT test for standard features unless they are specifically mentioned or modified
- If the ticket says "add a button", only test that button - don't test the entire page layout unless mentioned

**BEFORE ADDING EACH TEST CASE, ASK:**
1. "Is this feature explicitly mentioned in the ticket/PR/context?"
2. "Am I making assumptions based on what similar applications typically have?"
3. "Would someone reading the ticket description expect this test?"

If you answer "no" or "not sure" to question 1, DO NOT include that test case.

**EXAMPLES OF WHAT NOT TO DO:**
❌ Ticket: "Fix login button styling" → Don't add tests for password reset, OAuth, or session management
❌ Ticket: "Generate PDF report" → Don't add tests for watermarks, headers, footers unless mentioned
❌ Ticket: "Add export feature" → Don't test for file formats not mentioned in the ticket

**WHEN TO ADD "ABSENCE" TESTS:**
Only test for the absence of something if:
- The ticket explicitly mentions removing/hiding a feature
- The PR changes show deletion of code related to that feature
- The ticket description specifically says "without X" or "don't include X"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GENERATE TEST PLAN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Create a clear, actionable test plan organized by feature/component. Extract requirements from any format provided and focus on functional testing from a user perspective. REMEMBER: Only test what is explicitly mentioned in the requirements above.

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

**AVOID REDUNDANCY - CRITICAL RULE:**
DO NOT create separate test cases that test the same user flow from different angles. Instead, create ONE comprehensive test case that validates multiple aspects together.

❌ BAD - Redundant tests:
  - Test 1: "User clicks button and modal appears"
  - Test 2: "Modal posts to correct API endpoint"
  - Test 3: "API response includes correct user context"

✅ GOOD - Single comprehensive test:
  - Test 1: "User clicks button, modal appears, posts to correct API endpoint with proper context"

Combine related validations when they're part of the same user flow. Only create separate tests when:
- Testing different user paths (thumbs up vs thumbs down)
- Testing different entry points (task completion vs chat completion)
- Testing truly independent functionality

**Before adding a test case, ask yourself:** "Does another test already cover this user flow?"
If yes, enhance that existing test instead of creating a new one.

**INCLUDE THESE TEST TYPES:**

1. **Positive Scenarios (Happy Path)**
   - Test complete user flows from start to finish
   - VALIDATE MULTIPLE ASPECTS IN ONE TEST: UI behavior, API correctness, AND data integrity
   - Each test should be a comprehensive end-to-end validation, not just a UI check
   - Use specific examples from the ticket (not generic placeholders)

2. **Negative Scenarios (Error Handling)** - ONLY for error/edge cases
   - Test with invalid inputs, missing data, unauthorized access
   - Verify proper error messages and recovery mechanisms
   - Include specific examples: invalid email formats, wrong passwords, etc.
   - These should test DIFFERENT scenarios, not the same flow with valid data

3. **Edge Cases (Boundary Conditions)** - ONLY for boundaries and unusual inputs
   - Test minimum/maximum values, empty states, special characters
   - Focus on input validation and boundary handling
   - Do NOT duplicate happy path flows here

4. **Integration Scenarios** - ONLY when testing multi-system interactions
   - Use ONLY when testing interactions between separate systems/services
   - Do NOT use for standard features where UI calls a single API
   - Examples: Cross-service data flow, third-party integrations, microservice communication
   - If it's just "frontend → single backend API → database", that's a normal flow (use happy_path)

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
EXAMPLE - GOOD TEST ORGANIZATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**Scenario:** User feedback feature that posts to Slack

✅ CORRECT - 2 comprehensive tests in happy_path:
  1. "Complete thumbs up feedback flow with API validation"
     - Covers: UI modal appears → comment box displays → posts to correct endpoint → verifies Slack message contains user context
  2. "Complete thumbs down feedback flow with API validation"
     - Covers: UI modal appears → comment box displays → posts to correct endpoint → verifies Slack message contains user context

❌ INCORRECT - 6 redundant tests split across sections:
  Happy path:
    1. "Thumbs up displays comment box"
    2. "Thumbs down displays comment box"
  Edge cases:
    3. "Thumbs up posts to Slack"
    4. "Thumbs down posts to Slack"
  Integration tests:
    5. "Thumbs up uses correct API endpoint"
    6. "Thumbs down uses correct API endpoint"

The second approach tests the same flows 3 times each - wasteful and redundant!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Return ONLY valid JSON (no markdown, no code blocks):

{
  "happy_path": [
    {
      "title": "Comprehensive test name covering the complete flow",
      "priority": "critical|high|medium",
      "steps": [
        "First action step",
        "Second action step - verify intermediate state",
        "Third action step",
        "Fourth step - verify API correctness",
        "Fifth step - verify data integrity and context"
      ],
      "expected": "Complete expected outcome covering UI behavior, API correctness, and data validation",
      "test_data": "All specific data needed for this comprehensive test"
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

**CRITICAL ORDERING REQUIREMENT - READ THIS FIRST:**
YOU MUST ORDER ALL TEST CASES BY PRIORITY WITHIN EACH SECTION.
This is NON-NEGOTIABLE. The order MUST be:
  1. ALL "critical" priority tests FIRST
  2. ALL "high" priority tests SECOND
  3. ALL "medium" priority tests LAST

Apply this ordering to: happy_path, edge_cases, AND integration_tests sections.
DO NOT group tests by any other criteria (logical flow, dependencies, etc.).
PRIORITY ORDER OVERRIDES ALL OTHER CONSIDERATIONS.

Before generating each section, mentally sort your tests:
- Step 1: Identify all critical tests → put them first
- Step 2: Identify all high tests → put them after critical
- Step 3: Identify all medium tests → put them last

**RULES:**
- Steps array should contain plain action descriptions without numbering (numbering will be added during display)
- Priority values: "critical", "high", or "medium" (lowercase) - REQUIRED for all tests
- Categories: "security", "boundary", "error_handling", "integration"
- If integration_tests not needed, return empty array: []
- Use specific examples from the ticket, never generic placeholders
- All test_data should be concrete and specific

**FINAL CHECKLIST BEFORE GENERATING:**
✅ Every test case references something explicitly mentioned in the ticket/PR/context
✅ No tests for features that "should" exist but aren't actually mentioned
✅ No assumptions based on domain knowledge about what the application typically includes
✅ Tests are sorted by priority: critical → high → medium

Generate the test plan now. Remember: SORT BY PRIORITY FIRST and ONLY TEST WHAT IS EXPLICITLY MENTIONED."""
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
        images: list[tuple[str, str]] | None = None,
    ) -> TestPlan:
        """Generate test plan using Ollama."""
        # Note: Ollama doesn't support vision yet, so images are ignored
        if images:
            print("Warning: Ollama does not support image analysis. Images will be ignored.")

        prompt = self._build_prompt(
            ticket_key, summary, description, testing_context, development_info, has_images=bool(images)
        )

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
                        f"Failed to parse JSON response from Ollama: {e}",
                        error_type="service_unavailable"
                    ) from e

                return TestPlan(
                    happy_path=test_plan_data.get("happy_path", []),
                    edge_cases=test_plan_data.get("edge_cases", []),
                    regression_checklist=test_plan_data.get("regression_checklist", []),
                    integration_tests=test_plan_data.get("integration_tests", []),
                )

        except httpx.ConnectError as e:
            raise LLMError(
                f"Failed to connect to Ollama at {self.base_url}. Is Ollama running? Error: {e}",
                error_type="service_unavailable"
            ) from e
        except httpx.TimeoutException as e:
            raise LLMError(
                f"Ollama request timed out after 300s. Try a smaller model or increase timeout. Error: {e}",
                error_type="service_unavailable"
            ) from e
        except httpx.HTTPStatusError as e:
            raise LLMError(
                f"Ollama returned error status {e.response.status_code}: {e.response.text}",
                error_type="service_unavailable"
            ) from e


class ClaudeClient(LLMClient):
    """Claude API client (Anthropic, paid)."""

    def __init__(self):
        if not settings.anthropic_api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY not set. Please add it to your .env file to use Claude.",
                error_type="invalid"
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
        images: list[tuple[str, str]] | None = None,
    ) -> TestPlan:
        """Generate test plan using Claude API with optional image support."""
        prompt = self._build_prompt(
            ticket_key, summary, description, testing_context, development_info, has_images=bool(images)
        )

        # Build message content (text + images if provided)
        content = []

        # Add images first if available
        if images:
            for base64_data, media_type in images:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64_data,
                    },
                })

        # Add text prompt
        content.append({
            "type": "text",
            "text": prompt,
        })

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
                        "messages": [{"role": "user", "content": content}],
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
                        f"Response snippet: {response_text[max(0, e.pos-50):e.pos+50]}",
                        error_type="service_unavailable"
                    ) from e

                return TestPlan(
                    happy_path=test_plan_data.get("happy_path", []),
                    edge_cases=test_plan_data.get("edge_cases", []),
                    regression_checklist=test_plan_data.get("regression_checklist", []),
                    integration_tests=test_plan_data.get("integration_tests", []),
                )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                # Try to parse error message
                error_msg = ""
                try:
                    error_data = e.response.json()
                    error_msg = error_data.get("error", {}).get("message", "")
                except Exception:
                    pass

                if "invalid" in error_msg.lower():
                    raise LLMError(
                        "Anthropic API key is invalid. Please check your ANTHROPIC_API_KEY in .env or generate a new key at https://console.anthropic.com/settings/keys",
                        error_type="invalid"
                    ) from e
                else:
                    raise LLMError(
                        "Anthropic API authentication failed. Your API key may be expired or revoked. Get a new key at https://console.anthropic.com/settings/keys",
                        error_type="expired"
                    ) from e
            elif e.response.status_code == 429:
                raise LLMError(
                    "Anthropic API rate limit exceeded. Please wait and try again.",
                    error_type="rate_limited"
                ) from e
            raise LLMError(
                f"Claude API returned error status {e.response.status_code}: {e.response.text}",
                error_type="service_unavailable"
            ) from e
        except httpx.TimeoutException as e:
            raise LLMError(f"Claude API request timed out: {e}", error_type="service_unavailable") from e


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
            f"Unsupported LLM provider: {provider}. Use 'ollama' or 'claude'.",
            error_type="invalid"
        )
