"""
LLM client abstraction layer.

Supports multiple LLM providers with a unified interface:
- Ollama (local, free)
- Claude API (Anthropic, paid)

Switch providers by changing LLM_PROVIDER in .env
"""

import json
import re
from abc import ABC, abstractmethod
from dataclasses import is_dataclass
from typing import Any

import httpx

from .config import settings
from .models import BugAnalysis, TestPlan


def _safe_get(obj: dict | Any, key: str, default: Any = None) -> Any:
    """
    Safely get a value from either a dict or a dataclass instance.

    Args:
        obj: Either a dictionary or a dataclass instance
        key: The key/attribute name to access
        default: Default value if key doesn't exist

    Returns:
        The value from the object, or default if not found
    """
    if isinstance(obj, dict):
        return obj.get(key, default)
    elif is_dataclass(obj):
        return getattr(obj, key, default)
    return default


_VOICE_KEYWORDS_RE = re.compile(
    r"\b("
    r"voice[- ]?over|voiceover|"
    r"screen[- ]?reader|screenreader|"
    r"text[- ]?to[- ]?speech|speech[- ]?to[- ]?text|"
    r"speech recognition|"
    r"talkback|nvda|jaws|"
    r"dictat\w*|"
    r"tts|stt|"
    r"voice"
    r")\b",
    re.IGNORECASE,
)


def _is_voice_ticket(summary: str | None, description: str | None) -> bool:
    """Return True if the ticket involves voice I/O or screen-reader behavior.

    Word-boundary matching avoids false positives like 'invoice' or 'choice'.
    """
    combined = f"{summary or ''}\n{description or ''}"
    return bool(_VOICE_KEYWORDS_RE.search(combined))


VOICE_TESTING_GUIDANCE = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎙️ VOICE / SCREEN READER — SPECIALIZED TEST GUIDANCE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This ticket touches voice input, voice output, or screen-reader behavior. SKIP obvious cases the tester already knows to check (mic permission granted, labels exist, TTS produces sound). Focus on the non-obvious failure modes below, and ONLY include cases plausibly affected by what the ticket / diff actually changes. If the ticket is purely TTS, skip STT-specific cases, and vice versa.

**REQUIRED when the ticket involves streaming, concurrent audio, or both voice input and voice output in the same flow** — these two are frequently missed, include them unless the diff clearly rules them out:
- **Barge-in** — user speaks while the app is still talking. Does voice input get gated, does voice output cancel, or does the mic record the app's own voice output and transcribe it back as input? → `edge_cases` / `error_handling`.
- **Voice-output interruption policy** — a second voice-output trigger fires mid-utterance (rapid message, user re-asks, tool result arrives). Is it queued, cancel-and-replace, or overlapping into garbled audio? → `edge_cases` / `boundary`.

**Voice input (speech-to-text / dictation) — creative scenarios:**
- Mid-capture audio route change — Bluetooth headset connects/disconnects while dictating; does capture continue, restart, or silently drop?
- Domain vocabulary fidelity — proper nouns this feature actually uses (street names, MLS IDs, brokerage names, unit numbers like "3B" / "Apt 12-A") transcribe without autocorrect mangling.
- Numeric ambiguity — "three point five" vs "three fifty" vs "thirty-five hundred" vs "$3,500"; normalization must match the field type (currency vs year vs count vs decimal).
- Long silence mid-utterance — does auto-stop cut the user off while they're still thinking?
- Partial iOS permission — speech recognition granted but microphone denied (or vice versa); specific failure mode that bypasses the combined "permissions OK" path.
- Locale mismatch — device set to en-GB / es-MX while app forces en-US; which wins, and does recognition quality drop?

**Voice output (text-to-speech) — creative scenarios:**
- Pronunciation of the exact strings THIS feature emits — currency, HOA/MLS abbreviations, street addresses, ordinals, decimals, negative values.
- Audio focus — another app playing music or a call active; confirm ducking / pause / interrupt behavior is the intended choice.
- Rapid re-triggers — user taps the TTS control 5× quickly; queue bloat vs debounce vs cancel-and-restart.
- Silent mode / DND — is TTS still audible, and is that the intended behavior for this feature?

**Screen reader (VoiceOver / TalkBack / NVDA / JAWS) — creative scenarios:**
- Live-region + focus-change race — content updates while the reader is mid-sentence on the previous element; is the update lost or does it interrupt?
- Dynamic content mid-scan — list re-renders while the reader is scanning; does position survive?
- Custom gesture vs passthrough conflict — app-level swipe overlaps VoiceOver's passthrough gesture; which wins?
- Focus restoration after modal dismiss mid-announcement — lands on the correct trigger element?
- Text-expansion in localized strings (German, Finnish often 2–3× longer) — does visual truncation hide content from sighted users while the reader still reads the full string?
- iOS VoiceOver vs Android TalkBack parity — walk the same flow on each; flag any divergence.

━━ VOICE TEST-PLAN ORGANIZATION (MANDATORY) ━━
Voice tickets produce many tests. Keep the output scannable — PREFIX every test case title (in `happy_path`, `edge_cases`, and `integration_tests`) with ONE of these group tags:

  `[Prompts]`       agent dialogue quality: intro, clarification, confirmation, follow-ups
  `[Voice input]`   speech capture, transcription, mic handling, recognition quality
  `[Voice output]`  playback, pronunciation, audio focus, interruption policy
  `[Streaming]`     concurrent generation + playback, SSE / WebSocket transport, timing, barge-in
  `[UI]`            on-screen voice components (address cards, transcripts, settings sheet)
  `[Security]`      prompt injection, user-context sanitization, payload / rate caps
  `[Screen reader]` VoiceOver / TalkBack / NVDA / JAWS (only if the ticket touches a11y)

Use the tag text EXACTLY as shown above (including the spaces inside `[Voice input]` and `[Voice output]`). Do NOT abbreviate to `[STT]` or `[TTS]` — testers shouldn't have to decode acronyms.

Example title: `[Voice output] Agent reads captured currency amounts in natural language`.

**Ordering within each section:** first by priority (critical → high → medium) as already required, THEN keep same-group tests adjacent so the reader sees all `[Prompts]` together, all `[Voice input]` together, etc.

**Consolidate aggressively:** if multiple tests differ only by provider/model (e.g. xai vs whisper-1 vs gpt-4o-transcribe), collapse to ONE test with a provider/model table in `test_data`. Do NOT emit one test per provider. Same rule for different speech-to-text engines, text-to-speech voices, or LLM models.
"""


SYSTEM_PROMPT = """You are an expert QA engineer with 10+ years of experience creating comprehensive test plans. Your role is to generate thorough, actionable test cases that catch bugs before they reach production.

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
4. "If this test is scoped to a specific context (Buyer, Seller, etc.), does the ticket text or diff explicitly confirm this change applies to that context — or am I inferring it from a section heading?"

If you answer "no" or "not sure" to question 1 or 4, DO NOT include that test case.

**EXAMPLES OF WHAT NOT TO DO:**
❌ Ticket: "Fix login button styling" → Don't add tests for password reset, OAuth, or session management
❌ Ticket: "Generate PDF report" → Don't add tests for watermarks, headers, footers unless mentioned
❌ Ticket: "Add export feature" → Don't test for file formats not mentioned in the ticket

**DO NOT INVENT UI STATES OR OPTION VALUES:**
- NEVER assume a dropdown/selector has an "undefined", "empty", or "null" state unless the ticket explicitly says so
- NEVER test "leaving a field unselected" unless the ticket or context confirms the field can actually be empty (e.g. it has a placeholder like "Select an option" or the ticket mentions missing input handling)
- NEVER invent option values (e.g. "Buyer", "Seller", "Split") for a selector unless those exact options are listed in the ticket, PR diff, or testID reference
- If the ticket mentions a field/selector, only use the specific values explicitly named in the ticket description, acceptance criteria, or test data provided
❌ Ticket: "Handle Transfer Tax payor selection" → Don't test "undefined" payor unless the ticket explicitly describes that state

**DO NOT INVENT FORM FIELDS:**
- NEVER include a form field in test steps unless it is: (a) explicitly named in the ticket description, (b) visible in the testID reference or screen guide, or (c) confirmed in the PR diff
- Domain knowledge about what fields "should" exist in a real-world form is NOT a valid reason to include a field
- If you are not certain a field exists in this specific app, omit it
❌ The app is a real-estate calculator → Don't add "City Transfer Tax" or other domain-typical fields unless they are listed in the testID reference or ticket description

**SKIP OPTIONAL FIELDS WITH ACCEPTABLE DEFAULT VALUES:**
- When writing form-filling steps, ONLY include a field if entering a value is necessary to execute the test
- If a field has a default value that is acceptable for the scenario being tested, omit the step for that field entirely — do not instruct the tester to re-enter the default
- Entering a default value adds noise and makes tests harder to read without adding any verification value
❌ BAD: "Enter Seller Agent Fee: 3%" when 3% is the pre-populated default and the test is about VA-specific fees
✅ GOOD: Skip that step — the default is fine and the test is not about agent fees

**SCOPE INFERENCE FROM SECTION HEADINGS — CRITICAL:**
When a ticket uses a shared heading like "Defaults – Buyer & Seller" and lists items underneath it, DO NOT automatically assume every item applies to both Buyer AND Seller, or to a specific one of them.

Rules:
1. **Prefer code diffs over heading inference.** If diffs are provided, check whether the changed code touches buyer-specific paths, seller-specific paths, or both, for each individual item. Only generate tests for the contexts where the code actually changed.
2. **Do NOT split a single change into Buyer AND Seller tests unless the ticket explicitly says the change applies to both** (e.g., "apply to both buyer and seller defaults") or the diff confirms both paths were modified.
3. **Do NOT assign a Seller test to a feature/field that only appears in Buyer code** (or vice versa) even if the section heading mentions both.
4. If scope is genuinely ambiguous and no diff is available, use the most conservative interpretation: test the single combined flow described, and note the ambiguity in test_data rather than duplicating for each context.

❌ Ticket heading: "Defaults – Buyer & Seller" → Don't create a "Seller defaults" test for Hazard Insurance if Hazard Insurance only exists in Buyer defaults
✅ If diff shows HazardInsurance only changed in buyer-side files → generate test for Buyer defaults only
✅ If ticket explicitly says "applies to both buyer and seller" → generate tests for both

**IGNORE HISTORICAL / SUPERSEDED ACCEPTANCE CRITERIA:**
Ticket descriptions sometimes preserve old requirements under headings like "OG AC", "Old AC", "Original AC", "Previous AC", "Old Acceptance Criteria", or similar. These sections document what the requirements **used to be** — they are NOT current requirements.
- NEVER generate test cases based on content under these headings
- Treat them as historical context only; the current AC is everything outside those sections
❌ "OG AC: button should be red" → Do not test for a red button; the requirement has changed
✅ Look for the updated/current AC elsewhere in the description and test that instead

**WHEN TO ADD "ABSENCE" TESTS:**
Only test for the absence of something if:
- The ticket explicitly mentions removing/hiding a feature
- The PR changes show deletion of code related to that feature
- The ticket description specifically says "without X" or "don't include X"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ WHAT NOT TO TEST - BUILD-TIME vs RUNTIME
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**DO NOT CREATE TEST CASES FOR BUILD-TIME TOOLS OR CONFIGURATION:**
These are automatically validated by CI/build pipelines and don't require manual testing:

❌ ESLint configuration changes (eslint.config.js, .eslintrc, flat configs)
❌ Prettier/formatting configs
❌ TypeScript configuration (tsconfig.json compiler options)
❌ Build tool configs (webpack, vite, rollup, babel, esbuild)
❌ Package manager configs (package.json scripts, lockfiles, .npmrc)
❌ CI/CD pipeline configs (.github/workflows, .gitlab-ci.yml, Jenkinsfile)
❌ Development tooling (husky, lint-staged, commitlint)
❌ Test framework configs (jest.config.js, vitest.config.js)

**Why?** These fail the build automatically if broken. Manual testing adds no value.

**ONLY TEST RUNTIME BEHAVIOR:**
✅ App UI and functionality
✅ API endpoints and responses
✅ User authentication and authorization
✅ Data processing and validation
✅ Third-party integrations
✅ Performance and responsiveness
✅ Mobile/web app behavior on devices

**FOR SDK/DEPENDENCY UPDATES SPECIFICALLY:**
Focus on compatibility regression testing:
- Does the app still build and run?
- Do existing features still work with the new SDK version?
- Are there breaking changes from the SDK changelog that affect the app?

DO NOT test the features of the SDK itself - assume the SDK maintainers tested it.
DO NOT test that the build tools work - the build process itself validates this.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GENERATE TEST PLAN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Create a clear, actionable test plan organized by feature/component. Extract requirements from any format provided and focus on functional testing from a user perspective. REMEMBER: Only test what is explicitly mentioned in the requirements above.

**ADJUST SCOPE BASED ON COMPLEXITY:**
Analyze the ticket complexity and adjust test coverage accordingly:

- **SDK/Dependency Updates** (React 18→19, Node 18→20, Expo 52→53, library upgrades):
  - 3-4 happy path tests (app launches, core flows work with new SDK version)
  - 2-4 edge cases (breaking changes from SDK changelog/migration guide)
  - 4-6 regression items (existing features unaffected by upgrade)
  - Focus on COMPATIBILITY testing, not testing SDK features themselves
  - DO NOT test build tools (ESLint, TypeScript configs, bundler settings)
  - Example: "App launches on iOS with Expo SDK 53" NOT "ESLint v9 validates code"

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

**USE PRECONDITIONS TO ELIMINATE REPEATED SETUP STEPS:**
If 3 or more test cases in a section share the same opening steps (e.g. "Log in → navigate to listing → clear localStorage"), extract them into the `preconditions` field instead of repeating them in every steps list. Steps should begin at the actual variation point for that test.
- ❌ BAD: Every test starts with "Log in to Staging 2", "Navigate to a listing in an enabled state"
- ✅ GOOD: preconditions: "Logged in to Staging 2 with devagent account, on a listing in an enabled state"
- Only set preconditions when the setup is genuinely shared. If a test has unique setup (e.g. "clear localStorage" is only needed for 2 of 8 tests), keep those steps in the steps list.

**PARAMETERIZE IDENTICAL TEST CASES:**
If 3 or more test cases share identical steps and differ ONLY in test data (e.g. testing fallback behavior for states CT, KS, KY, LA), collapse them into ONE test case. Put the varying data in a markdown table inside the `test_data` field:
```
| State | Purchase Price |
|-------|---------------|
| CT    | $400,000      |
| KS    | $300,000      |
```
- ❌ BAD: Separate "Verify CT falls back gracefully", "Verify KS falls back gracefully", "Verify KY falls back gracefully" tests
- ✅ GOOD: One "Verify unsupported states fall back gracefully" test with a table of states in test_data

**USE TABLES FOR INPUT VALIDATION EDGE CASES:**
When multiple edge cases test the same input field with different invalid values (e.g. max boundary, negative, empty, decimal truncation), combine them into ONE test case. Use a markdown table in `test_data`:
```
| Input   | Trigger | Expected result     |
|---------|---------|---------------------|
| 25      | blur    | Clamped to 20%      |
| -5      | blur    | Restores to 6.75%   |
| (empty) | blur    | Restores to 6.75%   |
```
Only split into separate tests if the validation behavior is meaningfully different or requires different setup.

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
   - **DEDUPLICATION RULE:** If a behavior (e.g., "PDF excludes branding when flag is off") is already verified through a UI-based happy path test, do NOT create a separate integration/API test for the same behavior. The UI test already exercises the underlying API. Only add an integration test when it covers a genuinely different scenario or system interaction that no UI test reaches.
   - **API verification steps MUST always specify HOW to verify:**
     - If there is a UI-observable outcome, describe it: "Verify the Transfer Tax field is NOT shown on screen"
     - If there is NO UI outcome (pure backend/response check), always provide explicit DevTools steps: "Open browser DevTools (F12) > Network tab > filter for '[endpoint-name]' > trigger the action > click the request > inspect the Response tab and confirm [specific field/value]"
   - ❌ NEVER write vague steps like "Verify the API returns filtered sections" or "Verify integrationInfo shows GFE status" — these are untestable without specifying the verification mechanism

5. **Reset/Clear Functionality**
   - Test any reset, clear, or undo operations
   - Verify data is properly cleared/restored

**FORMAT EACH TEST AS: ACTION → EXPECTED RESULT**
Each test should include:
- Clear action steps (what the user does)
- Expected result (what should happen)
- Specific test data when needed

**CRITICAL: STEP ORDERING RULES**
- Steps must be in the exact sequential order a user would perform them in the UI
- Form submission/action buttons (`calculate`, `submit`, `confirm`, `save`, etc.) MUST always come AFTER all required inputs have been filled in
- Never place an action button tap in the middle of filling out a form — fill ALL inputs first, then tap the action button
- Think through the complete user flow before writing steps: enter all inputs → then trigger the action

**ADDITIVE OPERATIONS — CRITICAL RULE:**
When a requirement says "add X to Y", "the difference is added to Y", "increase Y by X", or any similar additive phrasing, the test MUST verify accumulation, not replacement. A common implementation bug is to SET Y = X instead of SET Y = Y + X. If your test data doesn't include a pre-existing value for Y, this bug will pass undetected.

Rules:
1. **Always set a non-zero starting value** for any field that is being added to. Never let it default to zero or blank when testing an additive operation.
2. **State the exact expected total** in the expected result: `final_Y = initial_Y + X`. Do NOT write vague phrases like "the X amount is included" or "Y reflects the difference" — these pass even when the field was replaced instead of accumulated.
3. **Include the arithmetic explicitly** in test_data so the tester can verify without guessing: e.g. `Initial Down Payment: $A + Excess: $B = Expected Total: $A+B`. Always compute the actual sum from your chosen test values — never copy a sum from an example.

❌ BAD — misses the replace-vs-add bug:
- test_data: "Purchase Price: $600,000, Max FHA: $500,000"
- expected: "Down Payment includes the $100,000 difference"
  → Passes even if Down Payment is SET to $100,000 (replacing the original value)

✅ GOOD — catches the replace-vs-add bug:
- test_data: "Initial Down Payment: $50,000, Purchase Price: $600,000, Max FHA: $500,000. Expected: $50,000 + $100,000 = $150,000"
- expected: "Down Payment field shows $150,000 (original $50,000 + $100,000 excess). A result of $100,000 would indicate the original value was replaced instead of accumulated."

Apply this rule to ALL additive scenarios: down payment accumulation, running totals, fee stacking, counter increments, balance additions, etc.

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
      "preconditions": "Assumed pre-state shared with other tests (omit if unique to this test)",
      "steps": [
        "First action step (start here, not at login/navigation if covered by preconditions)",
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

**REGRESSION CHECKLIST RULES:**
The regression checklist must contain ONLY runtime behaviors that can be manually tested.

❌ DO NOT INCLUDE build-time validations:
- "TypeScript compilation completes without errors"
- "ESLint validation passes"
- "Build succeeds with [SDK/tool version]"
- "App can be uploaded to App Store/Play Store"
- "No console warnings during build"

✅ ONLY INCLUDE runtime behaviors:
- "User authentication works correctly"
- "Navigation between screens functions properly"
- "Data saves and loads correctly"
- "Animations render smoothly"
- "API endpoints return expected data"

**Why?** Build-time checks fail automatically if broken. Regression checklists are for manually verifying existing features still work.

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
        comments: list[dict] | None = None,
        parent_info: dict | None = None,
        linked_info: dict | None = None,
        slack_messages: list[dict] | None = None,
    ) -> TestPlan:
        """Generate a structured test plan from ticket data and context.

        Args:
            images: List of (base64_data, media_type) tuples for image analysis
            comments: List of filtered testing-related Jira comments
            slack_messages: Resolved Slack messages from permalinks found in ticket
        """
        pass

    @abstractmethod
    async def generate_multi_ticket_test_plan(
        self,
        tickets: list[dict],
        images: list[tuple[str, str]] | None = None,
    ) -> TestPlan:
        """Generate a unified test plan from multiple related tickets.

        Args:
            tickets: List of ticket dicts with keys: ticket_key, summary, description,
                     issue_type, testing_context, development_info, comments,
                     parent_info, linked_info
            images: Combined image attachments from all tickets (up to 3)
        """
        pass

    @abstractmethod
    async def generate_bug_analysis(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        development_info: dict | None = None,
        comments: list[dict] | None = None,
        linked_info: dict | None = None,
        github_context: list[dict] | None = None,
    ) -> BugAnalysis:
        """Analyze a bug ticket and explain the bug, root cause, fix, and regression tests."""
        pass

    @abstractmethod
    async def generate_multi_bug_analysis(
        self,
        tickets: list[dict],
    ) -> BugAnalysis:
        """Analyze multiple related bug tickets together and produce a combined analysis."""
        pass

    @abstractmethod
    async def summarize_ticket(
        self,
        summary: str,
        description: str | None,
    ) -> str:
        """Return a 2-3 sentence plain-language summary of the ticket."""
        pass

    def _build_bug_analysis_prompt(self, tickets: list[dict]) -> str:
        """Build the prompt for bug analysis (single or multi-ticket)."""
        is_multi = len(tickets) > 1
        keys_str = ", ".join(t["ticket_key"] for t in tickets)

        if is_multi:
            prompt = f"Analyze the following {len(tickets)} related bug tickets together: {keys_str}.\n\n"
            prompt += "Produce a single combined analysis covering the shared root cause and fix.\n\n"
        else:
            ticket = tickets[0]
            prompt = f"Analyze this bug ticket: {ticket['ticket_key']}\n\n"

        for i, ticket in enumerate(tickets, 1):
            ticket_key = ticket["ticket_key"]
            summary = ticket["summary"]
            description = ticket.get("description")
            comments = ticket.get("comments")
            linked_info = ticket.get("linked_info")
            development_info = ticket.get("development_info")
            github_context = ticket.get("github_context")

            if is_multi:
                prompt += f"━━━ TICKET {i}: {ticket_key} ━━━\n"
            else:
                prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                prompt += "TICKET INFORMATION\n"
                prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

            prompt += f"\n**Ticket:** {ticket_key}\n"
            prompt += f"**Summary:** {summary}\n"
            prompt += f"\n**Description:**\n{description if description else 'No description provided'}\n"

            # Jira comments
            if comments:
                prompt += f"\n**Jira Comments ({len(comments)}):**\n"
                for comment in comments[:5]:
                    author = comment.get("author", "Unknown")
                    body = comment.get("body", "")
                    body_preview = body[:300] + "..." if len(body) > 300 else body
                    prompt += f"- @{author}: {body_preview}\n"

            # Linked issues (caused_by is most relevant for bugs)
            if linked_info:
                caused_by = linked_info.get("caused_by", [])
                if caused_by:
                    prompt += f"\n**Caused By:**\n"
                    for issue in caused_by:
                        prompt += f"- {issue.get('key')}: {issue.get('summary')}\n"

            # Development info (PRs + diffs — the heart of the analysis)
            if development_info:
                pull_requests = development_info.get("pull_requests", [])
                if pull_requests:
                    prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    prompt += "PULL REQUESTS & CODE CHANGES\n"
                    prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    open_prs = [pr for pr in pull_requests if (pr.get("status") or "").upper() == "OPEN"]
                    if open_prs:
                        prompt += (
                            f"\n⚠️ {len(open_prs)} of {len(pull_requests)} PR(s) are still OPEN — "
                            "the code in those PRs is unmerged and may still change. "
                            "Note this when reasoning about the fix state.\n"
                        )
                    for pr in pull_requests:
                        status = pr.get("status", "UNKNOWN")
                        merged = status.upper() in ("MERGED", "CLOSED")
                        prompt += f"\n**PR:** {pr.get('title', 'Untitled')} — Status: {status}"
                        if merged:
                            prompt += " ✅ (merged — bug is fixed)"
                        prompt += "\n"
                        if pr.get("source_branch"):
                            prompt += f"Branch: {pr['source_branch']}\n"
                        if pr.get("github_description"):
                            desc = pr["github_description"]
                            prompt += f"PR Description: {desc[:1000] + '...' if len(desc) > 1000 else desc}\n"

                        files_changed = pr.get("files_changed")
                        if files_changed:
                            sorted_files = sorted(files_changed, key=lambda f: f.get("changes", 0), reverse=True)
                            prompt += f"\nFiles changed ({len(files_changed)}):\n"
                            for fc in sorted_files[:15]:
                                icon = {"added": "✨", "modified": "📝", "removed": "🗑️", "renamed": "📛"}.get(fc.get("status", ""), "📄")
                                prompt += f"  {icon} {fc.get('filename', 'unknown')} (+{fc.get('additions', 0)}/-{fc.get('deletions', 0)})\n"

                            files_with_patches = [f for f in sorted_files if f.get("patch")]
                            if files_with_patches:
                                prompt += "\nCode diffs (use these to identify root cause and explain the fix):\n"
                                total_chars = 0
                                MAX_TOTAL = 16000
                                MAX_PER_FILE = 4000
                                for fc in files_with_patches:
                                    if total_chars >= MAX_TOTAL:
                                        break
                                    patch = fc.get("patch", "")
                                    fname = fc.get("filename", "unknown")
                                    if len(patch) > MAX_PER_FILE:
                                        patch = patch[:MAX_PER_FILE] + "\n...(truncated)"
                                    remaining = MAX_TOTAL - total_chars
                                    if len(patch) > remaining:
                                        patch = patch[:remaining] + "\n...(truncated)"
                                    prompt += f"\n--- {fname} ---\n"
                                    for line in patch.split("\n"):
                                        prompt += f"  {line}\n"
                                    total_chars += len(patch)

                        pr_comments = pr.get("comments")
                        if pr_comments:
                            prompt += f"\nPR Discussion ({len(pr_comments)} comments):\n"
                            for comment in pr_comments[:8]:
                                body = comment.get("body", "")
                                body_preview = body[:200] + "..." if len(body) > 200 else body
                                icon = "📝" if comment.get("comment_type") == "review_comment" else "💬"
                                prompt += f"  {icon} @{comment.get('author', 'unknown')}: {body_preview}\n"

            # GitHub context fetched from links in the ticket body
            if github_context:
                prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                prompt += "LINKED CODE CONTEXT (fetched from GitHub links in the ticket)\n"
                prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                prompt += "Use this code to inform root cause identification and fix complexity.\n\n"
                for item in github_context:
                    if item.get("type") == "file":
                        label = f"{item['path']}"
                        if item.get("lines"):
                            label += f" ({item['lines']})"
                        label += f" @ {item['ref']}"
                        prompt += f"**File: {label}**\n```\n{item['content']}\n```\n\n"
                    elif item.get("type") == "commit":
                        prompt += f"**Commit {item['sha']}:** {item['message']}\n"
                        for f in item.get("files", []):
                            icon = {"added": "✨", "modified": "📝", "removed": "🗑️", "renamed": "📛"}.get(f.get("status", ""), "📄")
                            prompt += f"  {icon} {f['filename']} (+{f['additions']}/-{f['deletions']})\n"
                            if f.get("patch"):
                                prompt += f"```diff\n{f['patch']}\n```\n"
                        prompt += "\n"

            prompt += "\n"

        prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        prompt += "Now submit your bug analysis using the submit_bug_analysis tool.\n"
        return prompt

    def _build_prompt(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        testing_context: dict,
        development_info: dict | None = None,
        has_images: bool = False,
        comments: list[dict] | None = None,
        parent_info: dict | None = None,
        linked_info: dict | None = None,
        slack_messages: list[dict] | None = None,
    ) -> str:
        """Build the prompt for test plan generation (shared across providers)."""
        prompt = f"""**Your Task:** Create a detailed test plan for the following Jira ticket{" (screenshots/mockups attached)" if has_images else ""}.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TICKET INFORMATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**Ticket:** {ticket_key}
**Summary:** {summary}

**Description:**
{description if description else "No description provided"}
"""

        # Add parent ticket context if available
        if parent_info:
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "PARENT TICKET CONTEXT\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += f"\n**This is a sub-task of:** {parent_info.get('key')} - {parent_info.get('summary')}\n"
            prompt += f"**Parent Type:** {parent_info.get('issue_type')}\n"

            parent_desc = parent_info.get('description')
            if parent_desc:
                # Truncate parent description if too long
                desc_preview = parent_desc[:1000] + "..." if len(parent_desc) > 1000 else parent_desc
                prompt += f"\n**Parent Description:**\n{desc_preview}\n"

            # Highlight parent resources
            parent_resources = []
            if parent_info.get('figma_context'):
                figma = parent_info['figma_context']
                file_name = _safe_get(figma, 'file_name', 'Unknown')
                parent_resources.append(f"📐 Figma design: {file_name}")
            if parent_info.get('attachments'):
                attachment_count = len(parent_info['attachments'])
                parent_resources.append(f"🖼️ {attachment_count} design image{'s' if attachment_count > 1 else ''}")

            if parent_resources:
                prompt += f"\n**Parent Resources:**\n"
                for resource in parent_resources:
                    prompt += f"- {resource}\n"

            prompt += "\n**Use parent context to:**\n"
            prompt += "- Understand the overall feature/epic this sub-task contributes to\n"
            prompt += "- Align test scenarios with parent-level business requirements and acceptance criteria\n"
            prompt += "- Use design specifications from parent Figma files and mockups\n"
            prompt += "- Validate that this sub-task fulfills its role in the broader feature\n"
            prompt += "- Consider integration points with other sub-tasks under the same parent\n"

        # Add linked issues context if available
        if linked_info:
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "LINKED ISSUES (DEPENDENCIES)\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

            # Show blocked_by issues (highest priority - these must be done first)
            blocked_by = linked_info.get('blocked_by', [])
            if blocked_by:
                prompt += f"\n**⛔ Blocked By ({len(blocked_by)} issue{'s' if len(blocked_by) > 1 else ''}):**\n"
                prompt += "This ticket CANNOT be tested until these are resolved:\n\n"
                for issue in blocked_by:
                    prompt += f"- **{issue.get('key')}**: {issue.get('summary')}\n"
                    if issue.get('status'):
                        prompt += f"  Status: {issue.get('status')}\n"
                    if issue.get('description'):
                        desc_preview = issue['description'][:200] + "..." if len(issue['description']) > 200 else issue['description']
                        prompt += f"  Description: {desc_preview}\n"
                    prompt += "\n"

            # Show blocks issues (test carefully - don't break downstream work)
            blocks = linked_info.get('blocks', [])
            if blocks:
                prompt += f"\n**🔒 Blocks ({len(blocks)} issue{'s' if len(blocks) > 1 else ''}):**\n"
                prompt += "This ticket blocks these downstream tickets - test thoroughly:\n\n"
                for issue in blocks:
                    prompt += f"- **{issue.get('key')}**: {issue.get('summary')}\n"
                    if issue.get('status'):
                        prompt += f"  Status: {issue.get('status')}\n"
                    if issue.get('description'):
                        desc_preview = issue['description'][:200] + "..." if len(issue['description']) > 200 else issue['description']
                        prompt += f"  Description: {desc_preview}\n"
                    prompt += "\n"

            # Show caused_by issues (root cause context)
            caused_by = linked_info.get('caused_by', [])
            if caused_by:
                prompt += f"\n**🐛 Caused By ({len(caused_by)} issue{'s' if len(caused_by) > 1 else ''}):**\n"
                prompt += "Root cause issues that led to this ticket:\n\n"
                for issue in caused_by:
                    prompt += f"- **{issue.get('key')}**: {issue.get('summary')}\n"
                    if issue.get('description'):
                        desc_preview = issue['description'][:200] + "..." if len(issue['description']) > 200 else issue['description']
                        prompt += f"  Description: {desc_preview}\n"
                    prompt += "\n"

            # Show causes issues (validate the fix doesn't cause downstream issues)
            causes = linked_info.get('causes', [])
            if causes:
                prompt += f"\n**⚠️ Causes ({len(causes)} issue{'s' if len(causes) > 1 else ''}):**\n"
                prompt += "This ticket may cause these issues - validate fixes don't regress:\n\n"
                for issue in causes:
                    prompt += f"- **{issue.get('key')}**: {issue.get('summary')}\n"
                    if issue.get('description'):
                        desc_preview = issue['description'][:200] + "..." if len(issue['description']) > 200 else issue['description']
                        prompt += f"  Description: {desc_preview}\n"
                    prompt += "\n"

            prompt += "**Use linked issues to:**\n"
            if blocked_by:
                prompt += "- ⚠️ CRITICAL: Validate that blocking issues are resolved before testing\n"
                prompt += "- Understand prerequisites and API contracts from blocking tickets\n"
            if blocks:
                prompt += "- Test thoroughly - downstream work depends on this being correct\n"
                prompt += "- Consider how changes might affect dependent tickets\n"
            if caused_by:
                prompt += "- Ensure the root cause is actually fixed, not just symptoms\n"
            if causes:
                prompt += "- Validate that fixes don't introduce regressions in related areas\n"
            prompt += "- Test integration points between this ticket and linked dependencies\n"

        # Add Jira comments if available
        if comments:
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "JIRA COMMENTS (TESTING-RELATED)\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += f"\nThe following {len(comments)} comment(s) from the Jira ticket contain testing discussions, edge cases, or scenarios:\n\n"

            for i, comment in enumerate(comments, 1):
                author = comment.get('author', 'Unknown')
                body = comment.get('body', '')
                created = comment.get('created', '')

                # Allow up to 8000 chars per comment to preserve full manual test plans.
                # Structured test plans written by developers can be several thousand chars
                # and must not be truncated, as every test case is meaningful.
                body_preview = body[:8000] + "..." if len(body) > 8000 else body

                prompt += f"**Comment {i} by {author}** (Posted: {created[:10] if created else 'Unknown date'}):\n"
                prompt += f"{body_preview}\n\n"

            prompt += "**Use these comments to:**\n"
            prompt += "- If a manual test plan is present, use it as the primary source of truth for test cases — preserve its structure, numbering, and coverage\n"
            prompt += "- Incorporate manually suggested test scenarios and edge cases\n"
            prompt += "- Address specific concerns or questions raised about testing\n"
            prompt += "- Include validation steps mentioned in the discussions\n"
            prompt += "- Consider any reproduction steps or test data mentioned\n\n"

        # Add resolved Slack discussions if available
        if slack_messages:
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "SLACK DISCUSSIONS (LINKED IN TICKET)\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += f"\nThe following {len(slack_messages)} Slack message(s) were linked from the ticket description or comments:\n\n"

            for i, msg in enumerate(slack_messages[:10], 1):
                author = _safe_get(msg, "author", "Unknown") or "Unknown"
                text = _safe_get(msg, "text", "") or ""
                url = _safe_get(msg, "url", "")
                # Cap each message to keep prompt size predictable; callers should
                # still include the URL so testers can read the full thread if needed.
                text_preview = text[:2000] + "..." if len(text) > 2000 else text
                prompt += f"**Slack message {i} by {author}:**\n"
                prompt += f"{text_preview}\n"
                if url:
                    prompt += f"(source: {url})\n"
                prompt += "\n"

            prompt += "**Use these Slack messages to:**\n"
            prompt += "- Incorporate edge cases, scenarios, or constraints raised in discussion\n"
            prompt += "- Treat them as supplementary context; the ticket itself remains the source of truth\n\n"

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

        # Add UI/simulator testing context if available.
        # These files are present on repos that use the simulator-testing skill pattern
        # (e.g. agent-calculator). Silently skipped for repos that don't have them.
        if development_info and development_info.get("repository_context"):
            repo_context = development_info["repository_context"]
            screen_guide = repo_context.get("screen_guide")
            testid_reference = repo_context.get("testid_reference")

            if screen_guide or testid_reference:
                prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                prompt += "UI NAVIGATION CONTEXT\n"
                prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                prompt += "\nThis app has stable testID identifiers on every interactive element. "
                prompt += "Use these in your test steps instead of generic descriptions.\n"
                prompt += "Example: write 'tap `price-input`' not 'tap the price field'.\n"

                if screen_guide:
                    # Include the navigation structure + first portion of screen descriptions.
                    # The full guide can be long; 4000 chars covers the structure overview
                    # and the most-tested screens.
                    guide_preview = screen_guide[:4000] + "\n...(truncated)" if len(screen_guide) > 4000 else screen_guide
                    prompt += f"\n**Screen Navigation Guide:**\n{guide_preview}\n"

                if testid_reference:
                    # Full reference maps every testID to its screen.
                    # 5000 chars covers the vast majority of screens.
                    ref_preview = testid_reference[:5000] + "\n...(truncated)" if len(testid_reference) > 5000 else testid_reference
                    prompt += f"\n**Available TestIDs by Screen:**\n{ref_preview}\n"

                prompt += "\n**Rules when using this context:**\n"
                prompt += "- Reference testIDs with backticks in action steps: `button-testid`\n"
                prompt += "- Only reference testIDs that appear in the list above\n"
                prompt += "- Use exact screen names from the guide for navigation steps\n"
                prompt += "- If a flow requires screens not in the guide, describe them generically\n"
                prompt += "- ⚠️ THE TESTID REFERENCE IS EXHAUSTIVE: every interactive element in the app has a testID listed above. If a form field or button does NOT appear in the reference, it does not exist in this app — do NOT invent steps for it, regardless of what domain knowledge suggests.\n"
                prompt += "- ⚠️ FORM FIELD COMPLETENESS: when writing form-filling steps, cross-check EVERY field against the testID reference. If you cannot find a matching testID for a field you are about to include, omit that step entirely.\n"

        # Add Figma design context if available (Phase 5)
        if development_info and development_info.get("figma_context"):
            figma_context = development_info["figma_context"]
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "DESIGN SPECIFICATIONS (FIGMA)\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            file_name = _safe_get(figma_context, 'file_name', 'Unknown')
            prompt += f"\n**Design File:** {file_name}\n"

            # Add frames/screens (limit to 30)
            frames = _safe_get(figma_context, "frames", [])
            if frames:
                prompt += f"\n**Screens/Frames ({len(frames)}):**\n"
                for frame in frames[:30]:
                    frame_name = _safe_get(frame, "name", "Unknown")
                    frame_type = _safe_get(frame, "type", "FRAME")
                    prompt += f"- {frame_name} ({frame_type})\n"

            # Add components (limit to 20)
            components = _safe_get(figma_context, "components", [])
            if components:
                prompt += f"\n**UI Components ({len(components)}):**\n"
                for comp in components[:20]:
                    comp_name = _safe_get(comp, "name", "Unknown")
                    comp_desc = _safe_get(comp, "description", None)
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

            # Add pull request information — open (unmerged) PRs are filtered out
            # so the test plan only reflects code that has actually landed.
            all_prs = development_info.get("pull_requests", [])
            pull_requests = [pr for pr in all_prs if (pr.get("status") or "").upper() != "OPEN"]
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
                        desc_preview = gh_desc[:1500] + "..." if len(gh_desc) > 1500 else gh_desc
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

                        # Show actual diff patches for runtime source files.
                        # Capped at 16000 chars total / 4000 chars per file so the prompt
                        # stays manageable while still exposing what was actually implemented.
                        files_with_patches = [f for f in sorted_files if f.get('patch')]
                        if files_with_patches:
                            prompt += "\n  📋 Key Code Changes (runtime files only):\n"
                            total_patch_chars = 0
                            MAX_TOTAL = 16000
                            MAX_PER_FILE = 4000
                            for fc in files_with_patches:
                                if total_patch_chars >= MAX_TOTAL:
                                    break
                                patch = fc.get('patch', '')
                                fname = fc.get('filename', 'unknown')
                                if len(patch) > MAX_PER_FILE:
                                    patch = patch[:MAX_PER_FILE] + "\n     ...(truncated)"
                                remaining = MAX_TOTAL - total_patch_chars
                                if len(patch) > remaining:
                                    patch = patch[:remaining] + "\n     ...(truncated)"
                                prompt += f"\n  --- {fname} ---\n"
                                for line in patch.split('\n'):
                                    prompt += f"  {line}\n"
                                total_patch_chars += len(patch)
                            prompt += "\n  ⚠️ REQUIRED: Read these diffs carefully and generate test cases for every new behaviour they introduce — especially new data sources, new fields, new API calls, and new conditional logic.\n"

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
                        prompt += "  ⚠️ REQUIRED: Generate specific test cases from the PR discussion above:\n"
                        prompt += "     - Each concern or question raised by a reviewer → create a test case that validates it\n"
                        prompt += "     - Each edge case or gotcha mentioned → create a test case that exercises it\n"
                        prompt += "     - Each bug or unexpected behavior noted → create a test case that catches regression\n"

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
            prompt += "- **FILTER OUT build-time changes**: Ignore ESLint configs, TypeScript configs, build tool settings, CI configs - focus ONLY on runtime code (UI components, API logic, business logic, data models)\n"
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

        if _is_voice_ticket(summary, description):
            prompt += VOICE_TESTING_GUIDANCE

        return prompt

    def _build_multi_ticket_prompt(
        self,
        tickets: list[dict],
        has_images: bool = False,
    ) -> str:
        """Build a combined prompt for multiple related tickets sharing code changes."""
        ticket_keys = [t["ticket_key"] for t in tickets]
        keys_str = ", ".join(ticket_keys)

        prompt = f"""**Your Task:** Create a single, unified, deduplicated test plan covering the following related Jira tickets that share code changes: {keys_str}.{"  (screenshots/mockups attached)" if has_images else ""}

Treat all tickets as parts of one combined feature. Do NOT produce separate test plans — generate ONE plan that covers the full scope.

"""
        # ── Per-ticket summaries ──────────────────────────────────────────────
        for i, ticket in enumerate(tickets, 1):
            ticket_key = ticket["ticket_key"]
            summary = ticket["summary"]
            description = ticket.get("description")

            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += f"TICKET {i} OF {len(tickets)}: {ticket_key}\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            prompt += f"**Summary:** {summary}\n\n"
            prompt += f"**Description:**\n{description if description else 'No description provided'}\n"

            parent_info = ticket.get("parent_info")
            if parent_info:
                prompt += f"\n**Parent Ticket:** {parent_info.get('key')} — {parent_info.get('summary')}\n"

            linked_info = ticket.get("linked_info")
            if linked_info:
                blocked_by = linked_info.get("blocked_by", [])
                if blocked_by:
                    prompt += f"**Blocked By:** {', '.join(b['key'] for b in blocked_by)}\n"

            comments = ticket.get("comments")
            if comments:
                prompt += f"\n**Testing Comments ({len(comments)}):**\n"
                for comment in comments[:3]:
                    body = comment.get("body", "")
                    body_preview = body[:300] + "..." if len(body) > 300 else body
                    prompt += f"- @{comment.get('author', 'Unknown')}: {body_preview}\n"

            prompt += "\n"

        # ── Shared development activity ───────────────────────────────────────
        tickets_with_dev = [t for t in tickets if t.get("development_info")]
        if tickets_with_dev:
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "SHARED DEVELOPMENT ACTIVITY\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

            for ticket in tickets_with_dev:
                dev_info = ticket["development_info"]
                ticket_key = ticket["ticket_key"]
                prompt += f"**{ticket_key} — Development:**\n"

                # Open (unmerged) PRs are excluded so the plan only reflects landed code.
                all_prs = dev_info.get("pull_requests", [])
                pull_requests = [pr for pr in all_prs if (pr.get("status") or "").upper() != "OPEN"]
                for pr in pull_requests:
                    prompt += f"- PR: **{pr.get('title', 'Untitled')}** ({pr.get('status', 'UNKNOWN')})\n"
                    if pr.get("source_branch"):
                        prompt += f"  Branch: {pr['source_branch']}\n"
                    if pr.get("github_description"):
                        desc = pr["github_description"]
                        prompt += f"  PR Description: {desc[:200] + '...' if len(desc) > 200 else desc}\n"

                    files_changed = pr.get("files_changed")
                    if files_changed:
                        total_add = pr.get("total_additions", 0)
                        total_del = pr.get("total_deletions", 0)
                        prompt += f"  📊 {len(files_changed)} files (+{total_add}/-{total_del})\n"
                        sorted_files = sorted(files_changed, key=lambda f: f.get("changes", 0), reverse=True)
                        prompt += "  📁 Files:\n"
                        for fc in sorted_files[:10]:
                            icon = {"added": "✨", "modified": "📝", "removed": "🗑️", "renamed": "📛"}.get(fc.get("status", ""), "📄")
                            prompt += f"     {icon} {fc.get('filename', 'unknown')} (+{fc.get('additions', 0)}/-{fc.get('deletions', 0)})\n"
                        if len(files_changed) > 10:
                            prompt += f"     ... and {len(files_changed) - 10} more files\n"

                        # Code diffs — smaller budget per ticket in multi-ticket mode
                        files_with_patches = [f for f in sorted_files if f.get("patch")]
                        if files_with_patches:
                            prompt += "\n  📋 Key Code Changes:\n"
                            total_patch_chars = 0
                            MAX_TOTAL = 8000
                            MAX_PER_FILE = 2000
                            for fc in files_with_patches:
                                if total_patch_chars >= MAX_TOTAL:
                                    break
                                patch = fc.get("patch", "")
                                fname = fc.get("filename", "unknown")
                                if len(patch) > MAX_PER_FILE:
                                    patch = patch[:MAX_PER_FILE] + "\n     ...(truncated)"
                                remaining = MAX_TOTAL - total_patch_chars
                                if len(patch) > remaining:
                                    patch = patch[:remaining] + "\n     ...(truncated)"
                                prompt += f"\n  --- {fname} ---\n"
                                for line in patch.split("\n"):
                                    prompt += f"  {line}\n"
                                total_patch_chars += len(patch)
                            prompt += "\n  ⚠️ REQUIRED: Read these diffs and generate test cases for every new behaviour introduced.\n"

                        prompt += "\n"

                    pr_comments = pr.get("comments")
                    if pr_comments:
                        prompt += f"  💬 PR Discussion ({len(pr_comments)} comments):\n"
                        for comment in pr_comments[:5]:
                            body = comment.get("body", "")
                            body_preview = body[:150] + "..." if len(body) > 150 else body
                            icon = "📝" if comment.get("comment_type") == "review_comment" else "💬"
                            prompt += f"     {icon} @{comment.get('author', 'unknown')}: {body_preview}\n"
                        prompt += "\n"

                commits = dev_info.get("commits", [])
                if commits:
                    prompt += f"  Commits ({len(commits)}):\n"
                    for commit in commits[:5]:
                        msg = commit.get("message", "No message").split("\n")[0]
                        prompt += f"  - {msg}\n"

                prompt += "\n"

            # UI navigation context — use first ticket that has it
            for ticket in tickets_with_dev:
                dev_info = ticket["development_info"]
                repo_context = dev_info.get("repository_context")
                if not repo_context:
                    continue
                screen_guide = repo_context.get("screen_guide")
                testid_reference = repo_context.get("testid_reference")
                if screen_guide or testid_reference:
                    prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    prompt += "UI NAVIGATION CONTEXT\n"
                    prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    prompt += "\nThis app has stable testID identifiers. Use them in test steps instead of generic descriptions.\n"
                    if screen_guide:
                        guide_preview = screen_guide[:3000] + "\n...(truncated)" if len(screen_guide) > 3000 else screen_guide
                        prompt += f"\n**Screen Navigation Guide:**\n{guide_preview}\n"
                    if testid_reference:
                        ref_preview = testid_reference[:3000] + "\n...(truncated)" if len(testid_reference) > 3000 else testid_reference
                        prompt += f"\n**Available TestIDs:**\n{ref_preview}\n"
                    prompt += "\n⚠️ THE TESTID REFERENCE IS EXHAUSTIVE: every interactive element has a testID listed above. If a form field does NOT appear in the reference, it does not exist in this app — do NOT invent steps for it.\n"
                break

        # ── Final instructions ────────────────────────────────────────────────
        prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        prompt += "INSTRUCTIONS\n"
        prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        prompt += "Generate ONE unified test plan that covers all tickets above:\n"
        prompt += "- Treat all tickets as parts of a single combined feature\n"
        prompt += "- Do NOT duplicate test cases — merge overlapping scenarios\n"
        prompt += "- Prioritise integration tests that cover how the tickets interact\n"
        prompt += "- Use shared development context to understand the full scope of changes\n"
        prompt += "- **FILTER OUT build-time changes**: focus ONLY on runtime behaviour\n"

        if has_images:
            prompt += "\n**Note:** Screenshots or mockups from one or more tickets are attached. Use them for UI-specific test cases.\n"

        if any(_is_voice_ticket(t.get("summary"), t.get("description")) for t in tickets):
            prompt += VOICE_TESTING_GUIDANCE

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
        comments: list[dict] | None = None,
        parent_info: dict | None = None,
        linked_info: dict | None = None,
        slack_messages: list[dict] | None = None,
    ) -> TestPlan:
        """Generate test plan using Ollama."""
        # Note: Ollama doesn't support vision yet, so images are ignored
        if images:
            print("Warning: Ollama does not support image analysis. Images will be ignored.")

        prompt = self._build_prompt(
            ticket_key, summary, description, testing_context, development_info, has_images=bool(images), comments=comments, parent_info=parent_info, linked_info=linked_info, slack_messages=slack_messages
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
                        "options": {"temperature": 0.1},
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

    async def generate_multi_ticket_test_plan(
        self,
        tickets: list[dict],
        images: list[tuple[str, str]] | None = None,
    ) -> TestPlan:
        """Generate a unified test plan for multiple related tickets using Ollama."""
        if images:
            print("Warning: Ollama does not support image analysis. Images will be ignored.")

        prompt = self._build_multi_ticket_prompt(tickets, has_images=False)

        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "format": "json",
                        "stream": False,
                        "options": {"temperature": 0.1},
                    },
                )
                response.raise_for_status()

                data = response.json()
                response_text = data.get("response", "")

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
                f"Ollama request timed out after 300s. Error: {e}",
                error_type="service_unavailable"
            ) from e
        except httpx.HTTPStatusError as e:
            raise LLMError(
                f"Ollama returned error status {e.response.status_code}: {e.response.text}",
                error_type="service_unavailable"
            ) from e

    async def generate_bug_analysis(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        development_info: dict | None = None,
        comments: list[dict] | None = None,
        linked_info: dict | None = None,
        github_context: list[dict] | None = None,
    ) -> BugAnalysis:
        """Analyze a bug ticket using Ollama."""
        return await self._ollama_bug_analysis([{
            "ticket_key": ticket_key,
            "summary": summary,
            "description": description,
            "development_info": development_info,
            "comments": comments,
            "linked_info": linked_info,
            "github_context": github_context,
        }])

    async def generate_multi_bug_analysis(self, tickets: list[dict]) -> BugAnalysis:
        """Analyze multiple bug tickets using Ollama."""
        return await self._ollama_bug_analysis(tickets)

    async def summarize_ticket(self, summary: str, description: str | None) -> str:
        """Return a plain-language summary using Ollama."""
        desc_part = f"\n\nDescription:\n{description}" if description else ""
        prompt = (
            f"Summarize this Jira ticket in 2-3 plain sentences that a tester can quickly read. "
            f"Focus on what the feature/bug is, what it affects, and what a tester needs to know. "
            f"No jargon, no bullet points.\n\nTitle: {summary}{desc_part}"
        )
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "stream": False,
                    },
                )
                response.raise_for_status()
                result = response.json()
                return result.get("response", "").strip()
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise LLMError(f"Cannot connect to Ollama at {self.base_url}", error_type="connection_failed") from e
        except httpx.HTTPStatusError as e:
            raise LLMError(f"Ollama returned error status {e.response.status_code}", error_type="service_unavailable") from e

    async def _ollama_bug_analysis(self, tickets: list[dict]) -> BugAnalysis:
        prompt = self._build_bug_analysis_prompt(tickets)
        schema = {
            "type": "object",
            "properties": {
                "bug_summary": {"type": "string"},
                "root_cause": {"type": "string"},
                "is_fixed": {"type": "boolean"},
                "fix_explanation": {"type": "string"},
                "regression_tests": {"type": "array", "items": {"type": "string"}},
                "similar_patterns": {"type": "array", "items": {"type": "string"}},
                "fix_complexity": {"type": "string"},
                "fix_effort_estimate": {"type": "string"},
                "fix_complexity_reasoning": {"type": "string"},
                "affected_flow": {"type": "array", "items": {"type": "string"}},
                "scope_of_impact": {"type": "array", "items": {"type": "string"}},
                "why_tests_miss": {"type": "string"},
                "is_regression": {"type": "boolean"},
                "regression_introduced_by": {"type": "string"},
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "open_questions": {"type": "array", "items": {"type": "string"}},
                "suspect_symbols": {"type": "array", "items": {"type": "string"}},
            },
        }
        full_prompt = BUG_LENS_SYSTEM_PROMPT + "\n\n" + prompt + "\n\nReturn ONLY valid JSON matching this schema: " + json.dumps(schema)

        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": full_prompt,
                        "format": "json",
                        "stream": False,
                        "options": {"temperature": 0.1},
                    },
                )
                response.raise_for_status()
                data = response.json()
                try:
                    parsed = json.loads(data.get("response", ""))
                except json.JSONDecodeError as e:
                    raise LLMError(f"Failed to parse JSON from Ollama: {e}", error_type="service_unavailable") from e

                return BugAnalysis(
                    bug_summary=parsed.get("bug_summary", ""),
                    root_cause=parsed.get("root_cause"),
                    is_fixed=parsed.get("is_fixed", False),
                    fix_explanation=parsed.get("fix_explanation"),
                    regression_tests=parsed.get("regression_tests", []),
                    similar_patterns=parsed.get("similar_patterns", []),
                    fix_complexity=parsed.get("fix_complexity"),
                    fix_effort_estimate=parsed.get("fix_effort_estimate"),
                    fix_complexity_reasoning=parsed.get("fix_complexity_reasoning"),
                    affected_flow=parsed.get("affected_flow"),
                    scope_of_impact=parsed.get("scope_of_impact"),
                    why_tests_miss=parsed.get("why_tests_miss"),
                    is_regression=parsed.get("is_regression"),
                    regression_introduced_by=parsed.get("regression_introduced_by"),
                    assumptions=parsed.get("assumptions"),
                    open_questions=parsed.get("open_questions"),
                    suspect_symbols=parsed.get("suspect_symbols") or None,
                )

        except httpx.ConnectError as e:
            raise LLMError(f"Failed to connect to Ollama at {self.base_url}: {e}", error_type="service_unavailable") from e
        except httpx.TimeoutException as e:
            raise LLMError(f"Ollama request timed out: {e}", error_type="service_unavailable") from e
        except httpx.HTTPStatusError as e:
            raise LLMError(f"Ollama returned error {e.response.status_code}: {e.response.text}", error_type="service_unavailable") from e


TEST_CASE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "priority": {"type": "string", "enum": ["critical", "high", "medium"]},
        "preconditions": {"type": "string", "description": "Assumed pre-state before step 1 (e.g. 'Logged in, Buyer toggle selected, Interest Rate modal open'). Only include setup that is shared/repeated across multiple tests. Omit if the test has unique setup steps."},
        "steps": {"type": "array", "items": {"type": "string"}},
        "expected": {"type": "string"},
        "test_data": {"type": "string"},
    },
    "required": ["title", "priority", "steps", "expected"],
}

SUBMIT_TEST_PLAN_TOOL = {
    "name": "submit_test_plan",
    "description": "Submit the structured test plan with all test cases.",
    "input_schema": {
        "type": "object",
        "properties": {
            "happy_path": {
                "type": "array",
                "items": TEST_CASE_SCHEMA,
            },
            "edge_cases": {
                "type": "array",
                "items": {
                    **TEST_CASE_SCHEMA,
                    "properties": {
                        **TEST_CASE_SCHEMA["properties"],
                        "category": {
                            "type": "string",
                            "enum": ["security", "boundary", "error_handling", "integration"],
                            "description": "security: auth/permissions/data exposure. boundary: min/max values, empty inputs, limits. error_handling: invalid input, disabled states, UI feedback on failure, persistence/reset behavior. integration: cross-service or multi-system interactions.",
                        },
                    },
                },
            },
            "integration_tests": {
                "type": "array",
                "items": TEST_CASE_SCHEMA,
            },
            "regression_checklist": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["happy_path", "edge_cases", "regression_checklist"],
    },
}


class ClaudeClient(LLMClient):
    """Claude API client (Anthropic, paid)."""

    def __init__(self):
        if not settings.anthropic_api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY not set. Please add it to your .env file to use Claude.",
                error_type="invalid"
            )

        self.api_key = settings.anthropic_api_key
        self.model = settings.llm_model or "claude-opus-4-6"

    async def generate_test_plan(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        testing_context: dict,
        development_info: dict | None = None,
        images: list[tuple[str, str]] | None = None,
        comments: list[dict] | None = None,
        parent_info: dict | None = None,
        linked_info: dict | None = None,
        slack_messages: list[dict] | None = None,
    ) -> TestPlan:
        """Generate test plan using Claude API with optional image support."""
        prompt = self._build_prompt(
            ticket_key, summary, description, testing_context, development_info, has_images=bool(images), comments=comments, parent_info=parent_info, linked_info=linked_info, slack_messages=slack_messages
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
                        "system": [
                            {
                                "type": "text",
                                "text": SYSTEM_PROMPT,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                        "messages": [{"role": "user", "content": content}],
                        "temperature": 0.1,
                        "tools": [SUBMIT_TEST_PLAN_TOOL],
                        "tool_choice": {"type": "tool", "name": "submit_test_plan"},
                    },
                )
                response.raise_for_status()

                data = response.json()
                tool_block = next(
                    (b for b in data["content"] if b.get("type") == "tool_use"),
                    None,
                )
                if tool_block is None:
                    raise LLMError(
                        "Claude did not return a tool_use block. Unexpected response format.",
                        error_type="service_unavailable",
                    )
                test_plan_data = tool_block["input"]

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

    async def generate_multi_ticket_test_plan(
        self,
        tickets: list[dict],
        images: list[tuple[str, str]] | None = None,
    ) -> TestPlan:
        """Generate a unified test plan for multiple related tickets using Claude API."""
        prompt = self._build_multi_ticket_prompt(tickets, has_images=bool(images))

        content = []

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

        content.append({"type": "text", "text": prompt})

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
                        "system": [
                            {
                                "type": "text",
                                "text": SYSTEM_PROMPT,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                        "messages": [{"role": "user", "content": content}],
                        "temperature": 0.1,
                        "tools": [SUBMIT_TEST_PLAN_TOOL],
                        "tool_choice": {"type": "tool", "name": "submit_test_plan"},
                    },
                )
                response.raise_for_status()

                data = response.json()
                tool_block = next(
                    (b for b in data["content"] if b.get("type") == "tool_use"),
                    None,
                )
                if tool_block is None:
                    raise LLMError(
                        "Claude did not return a tool_use block. Unexpected response format.",
                        error_type="service_unavailable",
                    )
                test_plan_data = tool_block["input"]

                return TestPlan(
                    happy_path=test_plan_data.get("happy_path", []),
                    edge_cases=test_plan_data.get("edge_cases", []),
                    regression_checklist=test_plan_data.get("regression_checklist", []),
                    integration_tests=test_plan_data.get("integration_tests", []),
                )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                error_msg = ""
                try:
                    error_data = e.response.json()
                    error_msg = error_data.get("error", {}).get("message", "")
                except Exception:
                    pass
                if "invalid" in error_msg.lower():
                    raise LLMError(
                        "Anthropic API key is invalid.",
                        error_type="invalid"
                    ) from e
                else:
                    raise LLMError(
                        "Anthropic API authentication failed.",
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

    async def generate_bug_analysis(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        development_info: dict | None = None,
        comments: list[dict] | None = None,
        linked_info: dict | None = None,
        github_context: list[dict] | None = None,
    ) -> BugAnalysis:
        """Analyze a bug ticket using Claude API."""
        return await self._claude_bug_analysis([{
            "ticket_key": ticket_key,
            "summary": summary,
            "description": description,
            "development_info": development_info,
            "comments": comments,
            "linked_info": linked_info,
            "github_context": github_context,
        }])

    async def generate_multi_bug_analysis(self, tickets: list[dict]) -> BugAnalysis:
        """Analyze multiple bug tickets using Claude API."""
        return await self._claude_bug_analysis(tickets)

    async def summarize_ticket(self, summary: str, description: str | None) -> str:
        """Return a plain-language summary using Claude API."""
        desc_part = f"\n\nDescription:\n{description}" if description else ""
        prompt = (
            f"Summarize this Jira ticket in 2-3 plain sentences that a tester can quickly read. "
            f"Focus on what the feature/bug is, what it affects, and what a tester needs to know. "
            f"No jargon, no bullet points. Reply with only the summary text.\n\nTitle: {summary}{desc_part}"
        )
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
                        "max_tokens": 256,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                    },
                )
                response.raise_for_status()
                result = response.json()
                return result["content"][0]["text"].strip()
        except httpx.HTTPStatusError as e:
            raise LLMError(f"Claude API returned error status {e.response.status_code}", error_type="service_unavailable") from e
        except httpx.TimeoutException as e:
            raise LLMError("Claude API request timed out", error_type="service_unavailable") from e

    async def _claude_bug_analysis(self, tickets: list[dict]) -> BugAnalysis:
        prompt = self._build_bug_analysis_prompt(tickets)

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
                        "max_tokens": 4096,
                        "system": [
                            {
                                "type": "text",
                                "text": BUG_LENS_SYSTEM_PROMPT,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "tools": [SUBMIT_BUG_ANALYSIS_TOOL],
                        "tool_choice": {"type": "tool", "name": "submit_bug_analysis"},
                    },
                )
                response.raise_for_status()

                data = response.json()
                tool_block = next(
                    (b for b in data["content"] if b.get("type") == "tool_use"),
                    None,
                )
                if tool_block is None:
                    raise LLMError("Claude did not return a tool_use block.", error_type="service_unavailable")

                parsed = tool_block["input"]
                return BugAnalysis(
                    bug_summary=parsed.get("bug_summary", ""),
                    root_cause=parsed.get("root_cause"),
                    is_fixed=parsed.get("is_fixed", False),
                    fix_explanation=parsed.get("fix_explanation"),
                    regression_tests=parsed.get("regression_tests", []),
                    similar_patterns=parsed.get("similar_patterns", []),
                    fix_complexity=parsed.get("fix_complexity"),
                    fix_effort_estimate=parsed.get("fix_effort_estimate"),
                    fix_complexity_reasoning=parsed.get("fix_complexity_reasoning"),
                    affected_flow=parsed.get("affected_flow"),
                    scope_of_impact=parsed.get("scope_of_impact"),
                    why_tests_miss=parsed.get("why_tests_miss"),
                    is_regression=parsed.get("is_regression"),
                    regression_introduced_by=parsed.get("regression_introduced_by"),
                    assumptions=parsed.get("assumptions"),
                    open_questions=parsed.get("open_questions"),
                    suspect_symbols=parsed.get("suspect_symbols") or None,
                )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise LLMError("Anthropic API key is invalid or expired.", error_type="invalid") from e
            elif e.response.status_code == 429:
                raise LLMError("Anthropic API rate limit exceeded.", error_type="rate_limited") from e
            raise LLMError(f"Claude API error {e.response.status_code}: {e.response.text}", error_type="service_unavailable") from e
        except httpx.TimeoutException as e:
            raise LLMError(f"Claude API request timed out: {e}", error_type="service_unavailable") from e


BUG_LENS_SYSTEM_PROMPT = """You are a senior software engineer performing a structured bug post-mortem. Your job is to analyze a Jira bug ticket — and any associated code changes — and produce a clear, grounded analysis.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT YOU MUST DO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. **bug_summary** — Explain the bug in plain English. What was the user-facing symptom? What was broken?

2. **root_cause** — If code diffs are available, identify the exact cause in the code. Reference file names and what the faulty logic was. If no diff is available, derive the likely cause from the ticket description and comments.

3. **is_fixed** — Set to true only if there is a merged pull request. Open or absent PRs mean the bug is not yet fixed.

4. **fix_explanation** — If fixed, explain what the code change did to resolve the bug. Reference specific files and the nature of the change. If not fixed, set to null.

5. **regression_tests** — List concrete, specific test cases a QA engineer can run to verify this exact bug does not recur. Each item must be a complete, actionable test description (not a category). Be specific: include the scenario, the input or action, and the expected outcome.

6. **similar_patterns** — List classes of related bugs that could exist elsewhere in the codebase based on the same root cause. These help the team proactively find similar issues.

7. **affected_flow** — A numbered list of steps tracing the end-to-end path from user action to the bug. Format each step as a short sentence, e.g. "1. User clicks Submit → 2. Frontend calls POST /api/calculate → 3. Handler calls FeeService.compute() → 4. compute() divides by zero when payor is null". If you cannot determine the flow from the available evidence, set to null.

8. **scope_of_impact** — Other callers or features affected by the same broken code. When code context is available (diffs, fetched files, linked code), each entry MUST name a specific file, component, or symbol (e.g. "apps/expo/src/screens/Folders.tsx uses the same BrandedHeader"). Screen names or feature categories alone ("Folders screen header") are only acceptable when no code context is available. Set to null if no other callers are identifiable from the evidence.

9. **why_tests_miss** — A single plain-English explanation of why the existing test suite did not catch this bug (e.g. mocking bypassed the broken layer, only happy-path covered, no integration test for this flow). If you cannot determine this from the evidence, set to null.

10. **is_regression** — Set to true if the bug was previously working and a specific code change broke it. Set to false if the feature was never functional. Set to null if you cannot determine this from the available evidence.

11. **regression_introduced_by** — If is_regression is true, identify the PR title, PR number, commit SHA, or branch name that introduced the breakage. Set to null if is_regression is false or unknown.

12. **fix_complexity** — Only when is_fixed is false. Classify the expected fix effort as one of:
   - "trivial": a one-liner or config change, no risk of side effects
   - "moderate": a focused code change in 1–2 files, straightforward logic fix
   - "complex": touches multiple files or services, requires careful testing
   - "architectural": requires design changes, schema migrations, or cross-team coordination
   Set to null if the bug is already fixed.

13. **fix_effort_estimate** — Only when is_fixed is false. A concise time range for a competent engineer who knows the codebase (e.g. "1–2 hours", "half a day", "2–3 days", "1+ week"). **If the scope is genuinely ambiguous (e.g. the ticket could mean a narrow UI tweak or a broader data-model change), give a branched estimate instead of averaging**, e.g. "2h if scoped to the existing single-color header / 4–5h if supporting per-rep colors end-to-end". Do not pick a single midpoint when the scope itself is unclear — the ambiguity belongs in the estimate. **Coupling with open_questions:** if `open_questions` contains ANY question about scope, feature breadth, or which alternative is intended, then `fix_effort_estimate` MUST be a branched estimate whose branches correspond to those alternatives. A single unconditional range is only valid when `open_questions` contains no scope-level questions. Widening a single range (e.g. "2–4 hours") is NOT an acceptable substitute for branching — it hides the ambiguity rather than exposing it. Set to null if the bug is already fixed.

14. **fix_complexity_reasoning** — Only when is_fixed is false. 1–2 sentences explaining why you assigned that complexity level. Reference specific files, services, or constraints. Set to null if the bug is already fixed.

15. **assumptions** — Inferences you made that are NOT directly grounded in the evidence, but that your analysis depends on. Example: "Assumed each Title Rep has their own assigned color (ticket says 'rep's assigned color' but code context only shows a single hardcoded value)." List every non-trivial leap so a reviewer can verify them. Set to null only if your analysis makes no such inferences.

16. **open_questions** — Interpretation ambiguities a human should resolve before committing to the estimate or fix. Phrase each as a question. Example: "Is the bug scoped to making the existing yellow header's contrast work, or does it include supporting arbitrary per-rep colors?" Set to null only if the scope is fully unambiguous from the evidence.

17. **suspect_symbols** — 1–3 code symbol names (component, function, class, or distinctive identifier) most likely implicated in the bug. These will be used to run a deterministic code search in the repo to produce a "Code Evidence" section, so pick names that are (a) likely to exist verbatim in the codebase and (b) specific enough to return meaningful hits. Good picks: `BrandedHeader`, `calculateAgentFee`, `useTitleRepColor`. Bad picks: generic words like `button`, `color`, `screen` (too many false matches). Prefer PascalCase component names and snake_case/camelCase function names over feature descriptions. Return an empty list (not null) if you cannot identify any specific symbols from the evidence — do NOT guess.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GROUNDING RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

- Base your analysis only on what is in the ticket, PR description, and code diffs provided.
- Do NOT invent root causes not supported by the evidence.
- Do NOT add regression tests for unrelated features.
- If a diff is not available, say so in root_cause and work from the ticket description only.
- Keep all text concise and technical — this is read by engineers and QA, not end users.
- For fix_complexity, fix_effort_estimate, and fix_complexity_reasoning: set all three to null when is_fixed is true.
- Surface ambiguity explicitly in assumptions and open_questions rather than resolving it silently. A confident-looking analysis that papers over interpretation gaps is worse than one that names them."""


SUBMIT_BUG_ANALYSIS_TOOL = {
    "name": "submit_bug_analysis",
    "description": "Submit the structured bug analysis.",
    "input_schema": {
        "type": "object",
        "properties": {
            "bug_summary": {
                "type": "string",
                "description": "Plain-English explanation of what the bug is and its user-facing impact.",
            },
            "root_cause": {
                "type": ["string", "null"],
                "description": "The technical root cause in the code. Reference file names if diffs are available.",
            },
            "is_fixed": {
                "type": "boolean",
                "description": "True only if a merged PR exists for this bug.",
            },
            "fix_explanation": {
                "type": ["string", "null"],
                "description": "What the fix did. Null if the bug is not yet fixed.",
            },
            "regression_tests": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete, specific test cases to prevent this bug from recurring.",
            },
            "similar_patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Classes of similar bugs to proactively look for in the codebase.",
            },
            "fix_complexity": {
                "type": ["string", "null"],
                "enum": ["trivial", "moderate", "complex", "architectural", None],
                "description": "Estimated fix complexity. One of: trivial, moderate, complex, architectural. Null if the bug is already fixed.",
            },
            "fix_effort_estimate": {
                "type": ["string", "null"],
                "description": "Estimated time to fix for a competent engineer (e.g. '2–4 hours', '1–2 days'). Null if the bug is already fixed.",
            },
            "fix_complexity_reasoning": {
                "type": ["string", "null"],
                "description": "1–2 sentences explaining the complexity rating. Reference files, services, or constraints. Null if the bug is already fixed.",
            },
            "affected_flow": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Numbered steps tracing the end-to-end path from user action to the bug. Each step is a short sentence. Null if flow cannot be determined.",
            },
            "scope_of_impact": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Other features, endpoints, or callers that invoke the same broken code and are also affected. Null if none identifiable.",
            },
            "why_tests_miss": {
                "type": ["string", "null"],
                "description": "Plain-English explanation of why existing tests did not catch this bug. Null if cannot be determined.",
            },
            "is_regression": {
                "type": ["boolean", "null"],
                "description": "True if the bug was previously working and a code change broke it. False if never functional. Null if unknown.",
            },
            "regression_introduced_by": {
                "type": ["string", "null"],
                "description": "PR title, PR number, commit SHA, or branch name that introduced the regression. Null if is_regression is false or unknown.",
            },
            "assumptions": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Non-trivial inferences the analysis depends on but that are NOT directly grounded in the ticket, comments, or code context. Each item names the inference so a reviewer can verify it. Null only if the analysis makes no such inferences.",
            },
            "open_questions": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Interpretation ambiguities a human should resolve before committing to the estimate or fix. Each item is phrased as a question. Null only if scope is fully unambiguous.",
            },
            "suspect_symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "1–3 specific code symbol names (component/function/class identifiers) likely implicated in the bug, for use in a deterministic code search. Return an empty list if no specific symbols are identifiable from the evidence — do not guess.",
            },
        },
        "required": ["bug_summary", "root_cause", "is_fixed", "fix_explanation", "regression_tests", "similar_patterns", "fix_complexity", "fix_effort_estimate", "fix_complexity_reasoning", "affected_flow", "scope_of_impact", "why_tests_miss", "is_regression", "regression_introduced_by", "assumptions", "open_questions", "suspect_symbols"],
    },
}


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
