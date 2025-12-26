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

The UI displays an "Additional Testing Context" form after fetching a ticket, with all fields optional. The form highlights recommended fields when description quality is weak.

### 4) LLM Prompt That Returns Structured JSON

Generate test plan suggestions reliably.

- [x] Prompt the LLM using:
  - Jira title + extracted description/AC
  - User-provided testing context
- [x] Require structured JSON output using a fixed schema
- [x] Validate the JSON response (fail gracefully if invalid)
- [x] Abstraction layer supporting multiple LLM providers (Ollama, Claude)
- [x] Easy provider switching via .env configuration

**MVP Output Schema:**

```json
{
  "happy_path": [{ "title": "", "steps": [], "expected": "" }],
  "edge_cases": [{ "title": "", "steps": [], "expected": "" }],
  "regression_checklist": [],
  "non_functional": [],
  "assumptions": [],
  "questions": []
}
```

### 5) Present Test Plan in UI + Export

Make it usable immediately.

- [ ] Render the plan in readable sections:
  - Happy Path
  - Edge Cases
  - Regression Checklist
  - Non-functional (if relevant)
  - Assumptions / Risks
  - Questions for PM/Dev
- [ ] "Copy as Markdown" button
- [ ] (Optional) "Download as .md" button

## Tech Stack

- **Backend:** Python + FastAPI
- **Frontend:** React with Vite
- **LLM:** Ollama (local, free) or Claude API (Anthropic, paid) - switchable via config
- **HTTP Client:** httpx
- **Config:** Pydantic Settings + `.env` locally; secrets manager later
- **Deployment (Phase 2):** Internal hosting + JumpCloud SSO

## Project Structure

```
src/
  app/
    main.py                # FastAPI app entrypoint
    jira_client.py         # Jira REST API client
    adf_parser.py          # Atlassian Document Format parser
    description_analyzer.py # Description quality analyzer
    llm_client.py          # LLM abstraction layer (Ollama + Claude)
    config.py              # environment configuration
frontend/
  src/
    App.jsx                # React UI component
    App.css                # Styles with dark/light theme
tests/
  test_manual.py           # Unit tests for ADF parser & analyzer
  test_api_mock.py         # API endpoint tests with mocks
  test_llm.py              # LLM integration test
  run_tests.py             # Simple test runner
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
   - `LLM_PROVIDER` - "ollama" (local) or "claude" (API)
   - `LLM_MODEL` - model name (e.g., "llama3.1" for Ollama, "claude-3-5-sonnet-20241022" for Claude)
   - `ANTHROPIC_API_KEY` - only if using Claude API

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

#### Option 1: Ollama (Local, Free) - Recommended for Development

1. Install Ollama from [https://ollama.com/download](https://ollama.com/download)
2. Start Ollama server: `ollama serve`
3. Pull a model: `ollama pull llama3.1`
4. In your `.env`, set:
   ```
   LLM_PROVIDER=ollama
   LLM_MODEL=llama3.1
   ```

**Test it:** `uv run python tests/test_llm.py`

#### Option 2: Claude API (Anthropic, Paid) - Best Quality

1. Get API key from your company's Anthropic account
2. In your `.env`, set:
   ```
   LLM_PROVIDER=claude
   LLM_MODEL=claude-3-5-sonnet-20241022
   ANTHROPIC_API_KEY=sk-ant-api03-your-key-here
   ```

**Test it:** `uv run python tests/test_llm.py`

#### Switching Providers

To switch between providers, just update `LLM_PROVIDER` in your `.env` file. No code changes needed!

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
  }
}
```

**Error responses:**

| Status | Meaning |
|--------|---------|
| 404 | Issue not found |
| 401 | Jira authentication failed |
| 403 | Jira access forbidden (permissions) |
| 502 | Jira unreachable or timed out |

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
    "riskAreas": "Email delivery, token generation"
  }
}
```

Returns structured test plan JSON with sections: `happy_path`, `edge_cases`, `regression_checklist`, `non_functional`, `assumptions`, `questions`.

**Error responses:**

| Status | Meaning |
|--------|---------|
| 503 | LLM service unavailable (Ollama not running, Claude API error) |

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
| UI Test Plan Rendering + Copy/Export | To Do |
| Internal MVP Demo | To Do |
| Phase 2 Planning (JumpCloud + Hosting) | To Do |

## Post-MVP Considerations (Phase 2+)

- JumpCloud SSO and internal hosting for company-wide access
- "Post back to Jira" as a button (manual write) before automation
- GitHub integration to incorporate changed files/repo areas
- Feedback loop (thumbs up/down per plan) to improve prompts
- Saved history of generated plans (per ticket) with audit trail

## License

See [LICENSE](LICENSE).
