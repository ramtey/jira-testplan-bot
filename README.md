# jira-testplan-bot

Generate a structured QA test plan from a Jira ticket using the ticket's title/description and user-provided supplemental testing info when Acceptance Criteria (AC) or key testing details are missing.

## MVP Goal

Provide a simple UI where a tester can enter a Jira ticket key, fetch ticket details, fill in missing context, and generate a structured, high-quality test plan that can be copied and used immediately.

### Non-goal (MVP)

Writing the test plan back into Jira (comment/custom field). This can be a Phase 2+ enhancement.

## MVP Features

### 1) Jira Integration (Read)

Fetch source data used for test plan generation.

- [x] Fetch Jira issue summary/title and description
- [x] Extract readable text from Jira description (best-effort; Jira often returns rich text)
- [x] Display a clear message when description/AC is missing or weak
- [x] **Fetch development activity** (commits, pull requests, branches) linked to the ticket
- [x] Display development information in the UI for additional context
- [x] Handle errors gracefully:
  - Ticket not found (404)
  - Auth failure / permissions (401/403)
  - Jira downtime/rate limiting (502)

### 2) UI: Ticket Input + Fetch + Review

Provide a lightweight workflow for testers.

- [x] Input field for Jira issue key (e.g., ABC-123)
- [x] "Fetch Ticket" button
- [x] Display:
  - Title
  - Description/AC (extracted text)
  - Labels
  - Issue type (color-coded: Story=green, Bug=red, Spike=blue, Task=light blue, Epic=purple)
  - **Development Activity** section showing:
    - Pull requests with status badges (MERGED, OPEN, etc.) and branch information
    - Commit count (e.g., "27 commits linked to this ticket")
    - Branch names
- [x] Clear loading state + friendly errors
- [x] Collapsible description (show more/less for long descriptions)
- [x] Dark/Light theme support (follows system preference)

### 3) Gap Detection + User-Supplied Testing Context

Improve output quality when Jira ticket content is incomplete.

When AC/testing info is missing or unclear, prompt the user to fill in:

- [x] Acceptance Criteria (if missing)
- [x] Test data notes (accounts, roles, sample data)
- [x] Environments (staging/prod flags, feature flags)
- [x] Roles/permissions involved
- [x] Out of scope / assumptions
- [x] Known risk areas / impacted modules
- [x] Special Testing Instructions (for complex multi-category scenarios)

The UI displays an "Additional Testing Context" form after fetching a ticket, with all fields optional. The form highlights recommended fields when description quality is weak.

**New:** For complex features with multiple categories or scenarios (e.g., keyword blocking with 50+ rules), use the "Special Testing Instructions" field to guide the LLM to generate specific test cases for each category. See [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md) for detailed examples.

### 4) LLM Prompt That Returns Structured JSON

Generate test plan suggestions reliably.

- [x] Prompt the LLM using:
  - Jira title + extracted description/AC
  - User-provided testing context
  - Special instructions for complex scenarios
- [x] Require structured JSON output using a fixed schema
- [x] Validate the JSON response (fail gracefully if invalid)
- [x] Abstraction layer supporting multiple LLM providers (Claude, Ollama)
- [x] Easy provider switching via .env configuration
- [x] Enhanced prompts that automatically detect and handle multi-category scenarios

**MVP Output Schema:**

```json
{
  "happy_path": [{ "title": "", "steps": [], "expected": "" }],
  "edge_cases": [{ "title": "", "steps": [], "expected": "" }],
  "regression_checklist": []
}
```

### 5) Present Test Plan in UI + Export

Make it usable immediately.

- [x] Render the plan in readable sections:
  - Happy Path (3-5 test cases)
  - Edge Cases (6-15+ test cases)
  - Regression Checklist (3-5 items)
- [x] "Copy for Jira" button (plain text format optimized for Jira comments)
- [x] "Copy as Markdown" button (universal Markdown format)
- [x] "Download as .md" button
- [x] Distinct button colors for easy identification (Green=Jira, Gray=Markdown, Blue=Download)

## Recent Improvements

### Smart Test Scope Filtering (Latest)
- **Build-time vs Runtime distinction**: Test plans now intelligently filter out build-time tools and configurations
  - Automatically skips test cases for ESLint, TypeScript configs, build tools, CI/CD configs, and development tooling
  - Focuses testing on actual runtime behavior (app UI, APIs, authentication, data processing)
  - Clear guidance: "These fail the build automatically if broken. Manual testing adds no value."
- **SDK/Dependency Update optimization**: Specialized handling for SDK and library upgrades
  - Reduced test scope: 3-4 happy path tests (vs 5-8 for complex features)
  - Focus on compatibility and regression testing, not testing SDK features themselves
  - Example: "App launches with Expo SDK 53" instead of "ESLint v9 validates code"
- **Development context filtering**: When analyzing PR file changes, automatically filters out non-runtime files
  - Ignores ESLint configs, TypeScript configs, build tool settings, and CI configs
  - Focuses only on runtime code: UI components, API logic, business logic, data models
- **Impact**: Eliminates irrelevant test cases for infrastructure tickets, reduces test plan generation time, and improves test quality

### Figma Design Context Integration
- **Automatic design specification enrichment**: Test plans now include actual UI component and screen names from Figma
  - Automatically extracts Figma URLs from Jira ticket descriptions (no manual input needed)
  - Fetches design file metadata, frames/screens (up to 50), and UI components (up to 30)
  - Enriches LLM prompt with actual design element names for UI-specific test cases
  - Supports `/file/`, `/design/`, and `/proto/` URL formats
  - Optional feature with graceful degradation (works without token)
  - Token validation integrated into health monitoring system
  - Example: Test cases reference "Login Screen" frame and "Email Input" component instead of generic "login page" and "input field"

### Token Health Monitoring
- **Comprehensive API token validation system**: Proactive monitoring and expiration detection for all API services
  - Centralized `TokenHealthService` validates Jira, GitHub, Claude, and Figma tokens
  - New `/health/tokens` API endpoint provides detailed status for each service
  - Enhanced error handling distinguishes between expired, invalid, rate-limited, and missing tokens
  - Frontend `TokenStatus` widget displays real-time status with visual indicators (✅/❌/ℹ️)
  - Auto-refreshes every 5 minutes with manual refresh option
  - Shows detailed error messages with direct links to token management pages
  - Displays authenticated user details when tokens are valid
  - Extensible architecture for easy addition of new services (Slack, OpenAI, etc.)
  - Better user experience: Clear remediation steps and help URLs for token issues

### Jira Comment Management
- **Smart comment replacement**: Test plans posted to Jira are now automatically updated instead of creating duplicates
  - First post creates a new comment with unique marker `🤖 Generated Test Plan`
  - Subsequent posts find and replace the existing test plan comment
  - User feedback shows "updated" vs "posted" status
  - Prevents comment clutter when regenerating test plans
  - Falls back to creating new comment if detection fails

### ADF Parser Fix
- **Fixed UUID extraction bug**: Removed incorrect extraction of internal Jira node IDs from descriptions
  - UUIDs like `53025c34-9388-4fcf-bf18-3c532e14a36f` no longer appear in extracted text
  - Only actual text content is extracted from Atlassian Document Format
  - Cleaner, more readable descriptions in UI and LLM context

### Priority-Based Test Ordering
- **Automatic test case ordering by priority**: All test cases are now automatically ordered by priority level within each section
  - Critical priority tests appear first (authentication, payments, data loss, security)
  - High priority tests appear second (core functionality, common flows, data integrity)
  - Medium priority tests appear last (edge cases, rare scenarios, minor issues)
  - Applies to: Happy Path, Edge Cases, and Integration Tests sections
  - Enables better test execution prioritization and faster identification of critical issues
  - LLM explicitly instructed to prioritize ordering over logical grouping

### Issue Type Validation
- **Smart issue type detection**: Automatically hides test plan generation for non-testable issue types
  - Disabled for: Epic, Spike, Sub-task (configurable)
  - Enabled for: Story, Task, Bug
  - Frontend shows friendly info message for non-testable types
  - Backend validates issue type and returns clear error if bypassed
  - Hybrid approach: Better UX + security validation

### Test Plan Output Optimization
- **Streamlined test plan structure**: Removed non-actionable sections for cleaner, more focused output
  - Removed: Non-functional tests, Assumptions, Questions for PM/Dev
  - Kept only: Happy Path, Edge Cases, Regression Checklist
  - Result: ~30% faster generation, 100% actionable content for testers
  - Lower API costs due to fewer tokens generated
- **Improved test plan quality**: Focus on concrete, executable test cases without speculative content

### LLM Integration with Development Context (Phase 2 Complete)
- **Intelligent test plan generation** using development activity:
  - LLM now receives commit messages, PR titles, and branch names from Jira
  - Automatically infers implementation details from development work
  - Generates more specific test cases based on actual code changes
  - Identifies risk areas from commit patterns (e.g., "authentication", "payment")
  - Focuses testing on modified areas
- **Streamlined user input**: Reduced form fields to only essential items (Acceptance Criteria + Special Instructions)
- **Claude API integration**: Using Claude 3.5 Sonnet for:
  - 5-10x faster response times (5-10 seconds vs 30-60+ seconds)
  - Superior quality test plans with better context understanding
  - More reliable JSON formatting
  - Better utilization of development information

### Development Activity Tracking (Phase 1 Complete)
- **Automatic fetching of development data** from Jira's dev-status API:
  - Pull requests with titles, status (MERGED, OPEN, DECLINED), URLs, and branch information
  - Commit messages and counts (e.g., "27 commits linked")
  - Branch names associated with the ticket
- **Visual display in UI** with:
  - Clickable PR titles linking directly to GitHub/Bitbucket
  - Color-coded status badges (green for merged/open, orange for declined/closed)
  - Professional card-based layout with hover effects
  - Responsive design for mobile and desktop
- **Non-blocking integration**: Development info fetch won't fail ticket loading if unavailable
- **Multi-platform support**: Works with GitHub, Bitbucket (Stash), and other Git integrations

### Enhanced Test Plan Generation
- **Smart multi-category detection**: Automatically generates specific test cases when tickets have multiple scenarios or rule categories
- **Special Instructions field**: Guide the LLM for complex features (see [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md))
- **Better coverage**: Increased test case counts (2-5 happy path, 3-6 edge cases)
- **Specific examples required**: LLM now uses concrete examples instead of generic placeholders

### Improved Jira Integration
- **Clickable ticket links**: Ticket numbers in the UI are clickable and open the Jira ticket in a new tab
- **Clean Jira formatting**: Plain text format that pastes cleanly into Jira comments (no more wiki markup issues)
- **Post to Jira button**: Directly post generated test plans as Jira comments with one click
- **Better button UX**: Distinct colors for each export button (Green=Jira, Gray=Markdown, Blue=Download)
- **Robust error handling**: Safe rendering even with malformed data

### Better Developer Experience
- See [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md) for comprehensive usage examples
- Template examples for different feature types (APIs, wizards, permissions, etc.)
- Clear guidance on when to use Special Instructions

## Tech Stack

- **Backend:** Python + FastAPI
- **Frontend:** React with Vite
- **LLM:** Claude API (Anthropic, recommended) or Ollama (local, free) - switchable via config
- **HTTP Client:** httpx
- **Config:** Pydantic Settings + `.env` locally; secrets manager later
- **Deployment (Phase 2):** Internal hosting + JumpCloud SSO

## Project Structure

```
src/
  app/
    main.py                 # FastAPI app entrypoint
    models.py               # Data models (Pydantic & dataclasses)
    jira_client.py          # Jira REST API client
    github_client.py        # GitHub API client (Phase 3a - PR enrichment)
    figma_client.py         # Figma API client (Phase 5 - Design context)
    token_service.py        # Token health monitoring service
    adf_parser.py           # Atlassian Document Format parser
    description_analyzer.py # Description quality analyzer
    llm_client.py           # LLM abstraction layer (Claude + Ollama)
    config.py               # Environment configuration
frontend/
  src/
    App.jsx                 # Main React app component
    App.css                 # Styles with dark/light theme
    config.js               # Frontend configuration (API URL)
    main.jsx                # React app entry point
    components/
      TicketForm.jsx        # Jira ticket input form
      TicketDetails.jsx     # Ticket display & quality analysis
      DevelopmentInfo.jsx   # Development activity display (PRs, commits, branches)
      TestingContextForm.jsx # Testing context input form
      TestPlanDisplay.jsx   # Test plan rendering & export
      TokenStatus.jsx       # API token health status widget
    utils/
      stateHelpers.js       # State management utilities
      markdown.js           # Markdown formatting utilities
tests/
  test_manual.py            # Unit tests for ADF parser & analyzer
  test_api_mock.py          # API endpoint tests with mocks
  test_llm.py               # LLM integration test
  run_tests.py              # Simple test runner
```

## Prerequisites

- Python 3.11+ recommended
- Node.js 20.19+ or 22.12+ (for frontend)
- uv installed

### Install uv

```bash
pip install uv
```

## Setup

### Backend Setup

1. Install dependencies:

```bash
uv sync
```

2. Create your local env file:

```bash
cp .env.example .env
```

3. Fill in `.env` values (do not commit `.env`):
   - `JIRA_BASE_URL`
   - `JIRA_EMAIL`
   - `JIRA_API_TOKEN`
   - `LLM_PROVIDER` - "claude" (recommended) or "ollama" (local)
   - `LLM_MODEL` - model name (e.g., "claude-sonnet-4-5-20250929" for Claude, "llama3.1" for Ollama)
   - `ANTHROPIC_API_KEY` - only if using Claude API
   - `GITHUB_TOKEN` - (optional) GitHub Personal Access Token for Phase 3a+3b+4 features:
     - Phase 3a: PR code diffs and file changes
     - Phase 3b: PR comments and review discussions
     - Phase 4: Repository documentation (README.md) and test file examples
     - **Important**: Token must be authorized for SAML SSO if your organization uses it
     - **Scopes needed**: `repo` (full control of private repositories)

### Frontend Setup

1. Navigate to frontend directory:

```bash
cd frontend
```

2. Install dependencies:

```bash
npm install
```

### LLM Setup (Choose One)

You have two options for the LLM provider:

#### Option 1: Claude API (Anthropic) - Recommended

1. Get API key from [https://console.anthropic.com/](https://console.anthropic.com/)
2. In your `.env`, set:
   ```
   LLM_PROVIDER=claude
   LLM_MODEL=claude-3-5-sonnet-20241022
   ANTHROPIC_API_KEY=sk-ant-api03-your-key-here
   ```

**Test it:** `uv run python tests/test_llm.py`

**Benefits:**
- 5-10x faster response times (5-10 seconds vs 30-60+ seconds)
- Superior quality test plans with better context understanding
- More reliable JSON formatting
- Better utilization of development information

#### Option 2: Ollama (Local, Free) - Alternative for Development

1. Install Ollama from [https://ollama.com/download](https://ollama.com/download)
2. Start Ollama server: `ollama serve`
3. Pull a model: `ollama pull llama3.1`
4. In your `.env`, set:
   ```
   LLM_PROVIDER=ollama
   LLM_MODEL=llama3.1
   ```

**Test it:** `uv run python tests/test_llm.py`

#### Switching Providers

To switch between providers, just update `LLM_PROVIDER` in your `.env` file. No code changes needed!

### GitHub Token Setup (Optional - for Enhanced Context)

The `GITHUB_TOKEN` enables Phase 3a, 3b, and 4 features that significantly improve test plan quality:
- Phase 3a: PR code diffs and file changes
- Phase 3b: PR comments and review discussions
- Phase 4: Repository documentation (README.md) and test file examples

**Without GitHub token**: Test plans still work but use only Jira data (basic PR titles and commits)
**With GitHub token**: Test plans include specific project terminology, data structures, and implementation details

#### Create GitHub Personal Access Token

1. Go to [GitHub Settings → Developer settings → Personal access tokens → Tokens (classic)](https://github.com/settings/tokens)
2. Click "Generate new token (classic)"
3. Give it a descriptive name (e.g., "jira-testplan-bot")
4. Select scopes:
   - ✅ `repo` (Full control of private repositories)
5. Click "Generate token"
6. Copy the token (starts with `ghp_`)
7. Add to your `.env` file:
   ```
   GITHUB_TOKEN=ghp_your_token_here
   ```

#### SAML SSO Authorization (Required for Enterprise Organizations)

If your organization uses SAML SSO (most companies do), you must authorize the token:

1. Go to [GitHub Settings → Personal access tokens](https://github.com/settings/tokens)
2. Find your newly created token in the list
3. Click "Configure SSO" next to the token
4. Click "Authorize" next to your organization name (e.g., `skyslope`)
5. Confirm the authorization

**Troubleshooting**: If you see "Resource protected by organization SAML enforcement" errors in logs:
- Your token needs SSO authorization
- Follow steps above to authorize the token for your organization
- Restart the backend server after authorization

## Run the Application

### Start Backend (Terminal 1)

```bash
uv run uvicorn src.app.main:app --reload
```

Backend runs on: `http://localhost:8000`

### Start Frontend (Terminal 2)

```bash
cd frontend
npm run dev
```

Frontend runs on: `http://localhost:5173`

### Health check

```
http://127.0.0.1:8000/health
```

### API docs (Swagger)

```
http://127.0.0.1:8000/docs
```

### Check API token health

```
GET http://127.0.0.1:8000/health/tokens
```

Returns health status for all configured API tokens:

```json
{
  "services": [
    {
      "service_name": "Jira",
      "is_valid": true,
      "is_required": true,
      "error_type": "valid",
      "error_message": null,
      "help_url": "https://support.atlassian.com/...",
      "last_checked": "2026-01-27T10:30:00.000Z",
      "details": {
        "user_email": "user@example.com",
        "user_name": "John Doe"
      }
    },
    {
      "service_name": "GitHub",
      "is_valid": true,
      "is_required": false,
      "error_type": "valid",
      "error_message": null,
      "help_url": "https://github.com/settings/tokens",
      "last_checked": "2026-01-27T10:30:00.000Z",
      "details": {
        "user_login": "johndoe"
      }
    },
    {
      "service_name": "Claude (Anthropic)",
      "is_valid": false,
      "is_required": true,
      "error_type": "expired",
      "error_message": "Anthropic API authentication failed. Your API key may be expired or revoked. Get a new key at https://console.anthropic.com/settings/keys",
      "help_url": "https://console.anthropic.com/settings/keys",
      "last_checked": "2026-01-27T10:30:00.000Z",
      "details": null
    }
  ],
  "overall_health": false
}
```

**Error types:**
- `valid` - Token is valid and working
- `missing` - Token not configured in .env
- `invalid` - Token is invalid or incorrectly formatted
- `expired` - Token has expired and needs renewal
- `rate_limited` - API rate limit exceeded (token is valid)
- `insufficient_permissions` - Token lacks required permissions
- `service_unavailable` - Service is unreachable or timed out

### Fetch a Jira issue

```
GET http://127.0.0.1:8000/issue/{issue_key}
```

Returns JSON:

```json
{
  "key": "PROJ-123",
  "summary": "Issue title",
  "description": "Issue description text (extracted from ADF format)",
  "labels": ["security", "user-management"],
  "issue_type": "Story",
  "description_quality": {
    "has_description": true,
    "is_weak": false,
    "warnings": [],
    "char_count": 245,
    "word_count": 42
  },
  "development_info": {
    "commits": [
      {
        "message": "Add password reset endpoint",
        "author": "John Doe",
        "date": "2026-01-05T10:55:58.000-0800",
        "url": "https://github.com/owner/repo/commit/abc123"
      }
    ],
    "pull_requests": [
      {
        "title": "SK-1782: Implement password reset",
        "status": "MERGED",
        "url": "https://github.com/owner/repo/pull/456",
        "source_branch": "feature/SK-1782-password-reset",
        "destination_branch": "main"
      }
    ],
    "branches": ["feature/SK-1782-password-reset"]
  }
}
```

**Error responses:**

| Status | Meaning |
|--------|---------|
| 404 | Issue not found |
| 401 | Jira authentication failed (token expired or invalid) |
| 403 | Jira access forbidden (insufficient permissions) |
| 502 | Jira unreachable or timed out |

**Note:** Error messages now distinguish between expired and invalid tokens, with direct links to token management pages.

### Generate a test plan

```
POST http://127.0.0.1:8000/generate-test-plan
```

Request body:

```json
{
  "ticket_key": "PROJ-123",
  "summary": "Add password reset functionality",
  "description": "Users should be able to reset their password via email...",
  "testing_context": {
    "acceptanceCriteria": "Given a user clicks 'Forgot Password'...",
    "testDataNotes": "Test with valid and invalid emails",
    "environments": "Staging and production",
    "rolesPermissions": "Any authenticated user",
    "outOfScope": "SSO password reset",
    "riskAreas": "Email delivery, token generation",
    "specialInstructions": "Test with specific examples for each error scenario"
  }
}
```

Returns structured test plan JSON with sections: `happy_path`, `edge_cases`, `regression_checklist`, `non_functional`, `assumptions`, `questions`.

**Error responses:**

| Status | Meaning |
|--------|---------|
| 503 | LLM service unavailable (Claude API error, token expired/invalid, or Ollama not running) |

**Note:** LLM errors now distinguish between expired, invalid, and rate-limited tokens with specific remediation guidance.

## Environments & Secrets

- Never commit `.env`
- Use `.env.example` as the template

## Testing Strategy

### Run tests

Quick test with dummy data (no dependencies):

```bash
uv run python tests/run_tests.py
```

Test LLM integration:

```bash
uv run python tests/test_llm.py
```

Run full test suite with pytest (optional):

```bash
uv add --dev pytest pytest-asyncio
uv run pytest tests/ -v
```

### Unit tests

- Jira client (mock responses)
- ADF parser (text extraction)
- Description analyzer (quality detection)
- LLM client (mock LLM responses)
- JSON schema validation
- Formatter/export (later)

### End-to-end testing

- Fetch → validate ticket → gather user inputs → LLM → render → copy/export
- Use a dedicated Jira test project (or safe test tickets) to avoid production impact

## Timeline and Milestones

| Milestone | Status |
|-----------|--------|
| Initial Setup (Repo, basic API + UI skeleton) | ✅ Done |
| Jira Read Integration Complete | ✅ Done |
| UI Fetch + Display Ticket | ✅ Done |
| Gap Detection + User Input Form | ✅ Done |
| LLM Integration + Backend Endpoint | ✅ Done |
| UI Test Plan Rendering + Copy/Export | ✅ Done |
| Development Activity Integration (Phase 1) | ✅ Done |
| Internal MVP Demo | ✅ Done |
| LLM Enhancement with Dev Context (Phase 2) | ✅ Done |
| Claude API Integration | ✅ Done |
| LLM Prompt Enhancements (Risk-based priorities, Given-When-Then format) | ✅ Done |
| Phase 3a (GitHub API Integration - PR Code Diffs) | ✅ Done |
| Phase 3b (GitHub PR Comments & Review Discussions) | ✅ Done |
| Phase 4 (Repository Documentation Context) | ✅ Done |
| Phase 5 (Figma Design Context Integration) | ✅ Done |
| Phase 3c Planning (Slack Bot Integration) | To Do |

## Post-MVP Considerations (Phase 3+)

### Jira Integration Enhancements
- "Post back to Jira" as a button (manual write) before automation
- Auto-populate testing context from previous similar tickets
- Fetch related tickets to include in test plan context

### Enhanced Version Control Integration (Phase 3)

**Current State (Phase 1-2 Complete):**
- ✅ Fetch commit messages, PR titles, and branch names from Jira
- ✅ Display development activity in UI with clickable links
- ✅ Show commit counts and PR statuses
- ✅ **Pass development info to LLM** for intelligent test plan generation
- ✅ LLM infers implementation details from commits and PR titles
- ✅ Automatic risk area identification from commit patterns
- ✅ Streamlined user input (only 2 fields: Acceptance Criteria + Special Instructions)

**Phase 3a Complete - GitHub PR Code Diffs:**
- ✅ **GitHub API Integration** for richer test plan context:
  - Fetch PR descriptions (often contain better acceptance criteria than Jira)
  - Extract actual code diffs and file changes from PRs
  - Identify which modules/files were modified with additions/deletions counts
  - Detect risk areas based on changed files (authentication, payment processing, etc.)
- ✅ **Enhanced LLM Context**:
  - Include file-level changes in LLM prompt (up to 15 most significant files)
  - Auto-detect testing scope from modified files
  - Generate specific test cases targeting modified files
  - Provide code change statistics (+additions/-deletions)
- ✅ **Implementation**:
  - Parse GitHub URLs from Jira's development info
  - Use GitHub Personal Access Token for API authentication (optional, graceful degradation)
  - Fetch PR details: `GET /repos/{owner}/{repo}/pulls/{number}`
  - Fetch file changes: `GET /repos/{owner}/{repo}/pulls/{number}/files`
  - Works across multiple repositories (any GitHub repo accessible with the token)
- ✅ **Backend-only enrichment**: Code changes sent to LLM but not displayed in UI for cleaner interface

**Phase 3b Complete - GitHub PR Comments & Discussions:**
- ✅ **PR Conversation Comments**:
  - Fetch discussion comments from PR conversation threads
  - Capture developer discussions about implementation decisions
  - Extract edge cases and concerns mentioned in comments
- ✅ **PR Review Comments**:
  - Fetch line-specific code review comments
  - Include file context for review comments (e.g., "[auth.js] Comment text")
  - Capture QA and code quality concerns from reviewers
- ✅ **Enhanced LLM Context**:
  - Include up to 10 most recent comments in LLM prompt
  - Extract testing insights from developer discussions
  - Identify edge cases and gotchas mentioned during code review
  - Use review feedback to generate more comprehensive test cases
- ✅ **Implementation**:
  - Fetch conversation comments: `GET /repos/{owner}/{repo}/issues/{number}/comments`
  - Fetch review comments: `GET /repos/{owner}/{repo}/pulls/{number}/comments`
  - Combine both comment types with appropriate icons and context
  - Graceful handling when PR has no comments

**Phase 4 Complete - Repository Documentation Context:**
- ✅ **README.md Integration**:
  - Automatically fetch README.md from repository's main branch
  - Include first 2000 characters in LLM prompt for project context
  - Understand project structure, architecture, and terminology
  - Use project-specific UI component names and navigation patterns
- ✅ **Test File Examples**:
  - Search for test files in repository (*.test.*, *.spec.*, __tests__, tests/)
  - Show LLM existing test patterns and conventions
  - Generate test cases that match project's testing style
- ✅ **Benefits**:
  - Test steps use **specific terminology** instead of generic placeholders
  - References actual screen names, button labels, menu items from README
  - Understands project architecture and tech stack
  - Generates test data matching real data structures from documentation
- ✅ **Example Impact**:
  - **Before**: "Navigate to a feature that generates a PDF document"
  - **After**: "Generate a seller net sheet PDF with titleAgentInfo set to null"
- ✅ **Implementation**:
  - Fetch README: `GET /repos/{owner}/{repo}/contents/README.md`
  - Search test files: `GET /search/code?q=repo:{owner}/{repo} extension:test OR path:tests`
  - Graceful degradation if README not found or no test files exist
  - Truncate long READMEs to avoid token limits

**Figma Design Context Integration (Phase 5 - Complete):**
- ✅ **Automatic Figma URL extraction from ticket descriptions**
- ✅ **Design file metadata, frames/screens, and UI components**
- ✅ **Test plans include actual component and screen names from Figma**
- ✅ **Token validation and health monitoring**
- See "Recent Improvements" section above for details

**Visual & Video Context Integration (Future):**
- **Screenshot Upload Support**:
  - Accept 1-2 images (PNG/JPG) in testing context form
  - Pass images to Claude API (multimodal vision capabilities)
  - Generate visual test cases based on UI mockups
  - Compare expected design vs actual screenshots
  - Identify visual differences and generate specific UI test scenarios
- **Loom Transcript Integration**:
  - Support Loom video transcript text input (pasted by user)
  - Automatic transcript extraction via Loom API or third-party scrapers (Apify)
  - Parse video context for implementation details and acceptance criteria
  - Extract visual demonstrations and user flows from video content
  - **Challenge**: Loom's official API focuses on recording SDK, not transcript access
  - **Workaround Options**:
    - Manual paste: User copies transcript from Loom UI (SRT download available)
    - Third-party APIs: Apify actors for automated transcript extraction
    - Future: Direct Loom API integration if transcript endpoint becomes available
- **GitHub PR Description Fetching**:
  - Fetch full PR descriptions from GitHub API (not just titles from Jira)
  - Extract detailed implementation notes often missing from Jira tickets
  - Parse PR comments and code review discussions
  - Identify acceptance criteria mentioned in PR descriptions
  - **Benefits**: PR descriptions typically contain better technical context than Jira
- **Implementation Approach**:
  - Frontend: Add file upload component for screenshots
  - Frontend: Add textarea for Loom transcript paste (or URL for future API integration)
  - Backend: Extend TestingContext model with `screenshots`, `loom_transcript`
  - LLM: Update prompt to include visual and video context
  - LLM: Use Claude's vision API for screenshot analysis
- **Benefits**:
  - Catch visual regressions and UI implementation differences
  - Leverage video demos for better test coverage
  - Extract requirements from multiple sources (Jira + PR + video + design)
  - Reduce miscommunication from poorly written tickets

**Slack Bot Integration (Phase 3c):**
- **Quick Test Plan Generation via Slash Command**:
  - Slash command: `/testplan TICKET-123` generates plan without leaving Slack
  - Auto-fetch ticket details and development info from Jira
  - Generate test plan using existing backend logic (no manual context input)
  - Post formatted plan directly in Slack channel (visible to team)
  - Supports basic special instructions: `/testplan SK-1234 special: Test all keyword categories`
- **Interactive Slack UI**:
  - Formatted output using Slack Block Kit (collapsible sections, rich formatting)
  - Action buttons:
    - "📋 View Full Plan" - Links to web UI with full details
    - "✨ Add Context & Regenerate" - Opens Slack modal for acceptance criteria input
    - "💬 Discuss in Thread" - Encourages team collaboration
  - Status updates: "⏳ Generating..." → "✅ Test plan ready"
- **Use Cases**:
  - **Quick wins**: Well-written tickets with good descriptions (~30% of tickets)
  - **Team collaboration**: Share plans in channels for immediate feedback
  - **Discovery**: Increases tool adoption without requiring web UI access
  - **Triage**: Quick assessment of testing scope during sprint planning
- **Implementation Approach**:
  - Create Slack App at api.slack.com with slash command configuration
  - New FastAPI endpoints:
    - `POST /slack/commands` - Handle slash command invocations
    - `POST /slack/interactions` - Handle button clicks and modal submissions
  - Slack signature verification for security
  - Reuse existing `generate_test_plan` logic (same backend, different client)
  - Format test plans as Slack Block Kit messages with collapsible sections
  - Store bot token and signing secret in environment config
- **Benefits**:
  - **Lower friction**: Generate plans without context switching
  - **Increased adoption**: Visible in Slack, easy to discover and use
  - **Team visibility**: Plans shared in channels for discussion
  - **Hybrid workflow**: Simple tickets via Slack, complex tickets via web UI
  - **Same quality**: Uses identical LLM prompts and Jira integration as web UI
- **Limitations (by design)**:
  - No screenshot upload (use web UI for visual context)
  - Limited context input (basic special instructions only)
  - Text-only formatting (no rich PR cards like web UI)
  - Best for tickets with decent descriptions (fallback to web UI for poor tickets)
- **Estimated Effort**: 4-6 hours for MVP (slash command + basic formatting)

### Quality & Feedback
- Feedback loop (thumbs up/down per plan) to improve prompts
- Analytics dashboard showing test plan usage and quality metrics
- A/B testing different prompts to optimize output quality

### Test Plan History & Reusability
- **Saved history of generated plans** with full audit trail:
  - Store ticket key, timestamp, generated test plan, and testing context used
  - View history of test plans for a specific ticket
  - Compare test plans across different versions/iterations
  - Export history as CSV or JSON
- **Reusable testing context templates**:
  - Save common testing context combinations as templates
  - Quick-load templates for similar ticket types (e.g., "Auth Flow", "Payment Feature", "UI Changes")
  - Share templates across team members
- **Search and filter history**:
  - Search by ticket key, date range, or keywords
  - Filter by ticket type, labels, or team member
  - Bookmark favorite test plans for quick reference

### Advanced Features
- Batch processing: Generate test plans for multiple tickets at once
- Integration with test management tools (TestRail, Zephyr, etc.)
- Custom LLM prompt templates per team or project
- Automated test case generation from test plans
- Link to existing test automation frameworks

## Tips for Best Results

### Start Simple - Let the System Work
Just enter the Jira ticket key and click "Generate Test Plan". The enhanced system automatically:
- **Fetches development activity** from Jira (commits, PRs, branches)
- **Analyzes implementation details** from commit messages and PR titles
- **Identifies risk areas** from code change patterns
- Detects multiple categories in the ticket description
- Identifies behavior patterns (e.g., block vs allow continue)
- Extracts specific examples from the ticket
- Generates comprehensive test coverage (3-5 happy path, 6-10 edge cases for complex features)

**With Phase 2 complete, the system now generates smarter test plans with minimal input!**

### When to Use Special Instructions
Only use the **Special Testing Instructions** field if:
- The automatic generation missed critical test scenarios
- You need to emphasize specific priorities not clear from the ticket
- The ticket structure is unusual

Keep it simple - a few sentences is enough:
```
Focus on testing all keyword categories mentioned.
Test both hard block (racism) and soft block (FNF mentions) behaviors.
```

See [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md) for detailed guidance.

### Export Tips
- **Copy for Jira**: Use this for pasting directly into Jira comments - plain text with visual separators
- **Copy as Markdown**: Use for GitHub issues, Slack, or other tools that support Markdown
- **Download as .md**: Save for documentation, sharing via email, or version control

## Documentation

Additional documentation is available in the [`docs/`](docs/) folder:

- **[Testing Guide](docs/TESTING_GUIDE.md)** - Detailed guide on using the "Special Testing Instructions" field for complex features
- **[Prompt Improvements](docs/PROMPT_IMPROVEMENTS.md)** - Technical details on LLM prompt enhancements (risk-based priorities, Given-When-Then format, test data requirements)

## License

See [LICENSE](LICENSE).
