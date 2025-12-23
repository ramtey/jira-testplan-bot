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

- [ ] Input field for Jira issue key (e.g., ABC-123)
- [ ] "Fetch Ticket" button
- [ ] Display:
  - Title
  - Description/AC (extracted text)
  - Labels (optional)
  - Issue type (optional)
- [ ] Clear loading state + friendly errors

### 3) Gap Detection + User-Supplied Testing Context

Improve output quality when Jira ticket content is incomplete.

When AC/testing info is missing or unclear, prompt the user to fill in:

- [ ] Acceptance Criteria (if missing)
- [ ] Test data notes (accounts, roles, sample data)
- [ ] Environments (staging/prod flags, feature flags)
- [ ] Roles/permissions involved
- [ ] Out of scope / assumptions
- [ ] Known risk areas / impacted modules

Even if data is missing, the tool should still generate a plan and include a "Questions/Assumptions" section.

### 4) LLM Prompt That Returns Structured JSON

Generate test plan suggestions reliably.

- [ ] Prompt the LLM using:
  - Jira title + extracted description/AC
  - User-provided testing context
- [ ] Require structured JSON output using a fixed schema
- [ ] Validate the JSON response (fail gracefully if invalid)

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
- **Frontend:** React (or basic HTML first)
- **LLM:** Any approved provider (or local model for dev)
- **HTTP Client:** httpx
- **Config:** Pydantic Settings + `.env` locally; secrets manager later
- **Deployment (Phase 2):** Internal hosting + JumpCloud SSO

## Project Structure

```
src/
  app/
    main.py        # FastAPI app entrypoint
    jira_client.py # Jira REST API client
    config.py      # environment configuration
tests/
```

## Prerequisites

- Python 3.11+ recommended
- uv installed

### Install uv

```bash
pip install uv
```

## Setup

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
   - (later) `LLM_API_KEY`

## Run the API locally

```bash
uv run uvicorn src.app.main:app --reload
```

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

## Environments & Secrets

- Never commit `.env`
- Use `.env.example` as the template

## Testing Strategy

### Run tests

Quick test with dummy data (no dependencies):

```bash
uv run python tests/run_tests.py
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
- JSON schema validation (later)
- Formatter/export (later)

### End-to-end testing

- Fetch → validate ticket → gather user inputs → LLM → render → copy/export
- Use a dedicated Jira test project (or safe test tickets) to avoid production impact

## Timeline and Milestones

| Milestone | Status |
|-----------|--------|
| Initial Setup (Repo, basic API + UI skeleton) | ✅ Done |
| Jira Read Integration Complete | ✅ Done |
| UI Fetch + Display Ticket | To Do |
| Gap Detection + User Input Form | To Do |
| LLM Prompting + Schema Finalized | To Do |
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
