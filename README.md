# jira-testplan-bot

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)
![React](https://img.shields.io/badge/react-18.3-blue.svg)

Generate a structured QA test plan from a Jira ticket using the ticket's title/description and linked development activity (commits, PRs, code changes).

## 🔒 Security Notice

**IMPORTANT: Never commit your `.env` file!** It contains sensitive API tokens.

- Copy `.env.example` to `.env` and fill in your credentials
- The `.env` file is in `.gitignore` - keep it there
- Each user needs their own API tokens (Jira, Claude, GitHub, Figma)

## Overview

Generate structured QA test plans from Jira tickets by automatically analyzing:
- Ticket details and development activity (commits, PRs, branches)
- **Parent ticket context** (for sub-tasks: Epic/Story descriptions, Figma designs, images)
- **Linked ticket dependencies** (blocks, blocked by, causes, caused by)
- Jira comments with testing discussions and suggested scenarios
- GitHub PR code changes, comments, and repository documentation
- Figma design specifications (when available)
- Repository test patterns and conventions

**Features:**
- Web UI for browser-based workflows
- CLI tool for terminal-native workflows
- MCP server for Claude desktop integration
- Multiple export formats (Markdown, Jira, JSON)
- Token health monitoring and validation
- Post test plans directly to Jira comments
- **Parent ticket awareness** for sub-tasks to understand broader feature context

## Key Features

### Intelligent Context Analysis
- **Automatic context gathering**: Fetches ticket details, PRs, commits, code changes, and repository docs
- **Parent ticket awareness**: Automatically fetches parent Epic/Story context for sub-tasks, including:
  - Parent descriptions and business requirements
  - Figma designs attached to parent tickets
  - Design mockups/screenshots from parent
  - Overall feature context that sub-tasks lack
- **Linked ticket dependencies**: Automatically fetches blocking and dependency relationships:
  - Issues this ticket blocks (test thoroughly - others depend on this)
  - Issues blocking this ticket (prerequisites that must be resolved first)
  - Root cause issues (for bugs - ensures actual cause is fixed)
  - Downstream issues this ticket may cause (validate no regressions)
- **Smart comment analysis**: Extracts testing-related Jira comments (test scenarios, edge cases, QA discussions)
- **Figma integration**: Extracts actual UI component names from design files for specific test cases
- **Smart filtering**: Focuses on runtime behavior, ignoring build-time configs (ESLint, TypeScript, etc.)
- **Priority ordering**: Critical tests first, edge cases last

### Development Integration
- **GitHub enrichment**: PR code diffs (actual source changes injected into LLM context), review comments, and repository documentation
- **Simulator test context**: Automatically pulls testID references and screen guides from `.agents/skills/simulator-testing/references/` in the target repo (when present), so Claude references real UI test IDs in generated test steps
- **Jira development data**: Commits, branches, and PR statuses with clickable links
- **Token health monitoring**: Real-time validation with expiration warnings

### Test Plan Generation
- **Claude Opus 4.5**: Fast (5-10s), high-quality test plans with structured JSON output
- **Smart comment management**: Updates existing Jira comments instead of creating duplicates
- **Multiple export formats**: Markdown, Jira-formatted text, or JSON
- **Issue type validation**: Only generates plans for testable types (Story, Bug, Task)

## Tech Stack

**Backend:** Python, FastAPI, httpx
**Frontend:** React, Vite
**LLM:** Claude API (Anthropic) or Ollama
**CLI:** Typer, Rich, PyYAML

## Project Structure

- `src/app/` - Backend (FastAPI, Jira/GitHub/Figma clients, LLM integration)
- `src/cli/` - CLI tool (Typer, configuration management)
- `frontend/` - React web UI with Vite
- `tests/` - Unit and integration tests

## Prerequisites

- Python 3.11+
- Node.js 20+ (for web UI)
- `uv` package manager: `pip install uv`

## Setup

### Backend Setup

```bash
uv sync
cp .env.example .env
# Edit .env with your API tokens
```

Required: `JIRA_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN`, `ANTHROPIC_API_KEY`
Optional: `GITHUB_TOKEN`, `FIGMA_TOKEN`

### Frontend Setup

```bash
cd frontend
npm install
```

### LLM Setup

**Using Claude API** (recommended):
1. Get API key from [console.anthropic.com](https://console.anthropic.com/)
2. Add to `.env`:
   ```
   LLM_PROVIDER=claude
   LLM_MODEL=claude-opus-4-5-20251101
   ANTHROPIC_API_KEY=sk-ant-api03-...
   ```

**Alternative**: Ollama (local, free) - set `LLM_PROVIDER=ollama` and `LLM_MODEL=llama3.1` in `.env`

### GitHub Token Setup (Optional)

Enables PR code diffs, review comments, and repository documentation for better test plans.

1. Go to [GitHub Settings → Tokens](https://github.com/settings/tokens)
2. Generate new token with `repo` scope
3. Add to `.env`: `GITHUB_TOKEN=ghp_...`
4. **If using enterprise**: Authorize SSO for your organization

Without GitHub token, test plans use only Jira data (basic PR titles and commits).

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

## CLI Usage (Alternative to Web UI)

The CLI provides a fast, terminal-native way to generate test plans without running the web server.

### Installation

**For teams** (one-liner install):
```bash
curl -sSL https://raw.githubusercontent.com/your-org/jira-testplan-bot/main/install.sh | bash
```

**Or install directly** (if you have `uv`):
```bash
uv tool install git+https://github.com/your-org/jira-testplan-bot.git
```

**For local development**:
```bash
git clone https://github.com/your-org/jira-testplan-bot.git
cd jira-testplan-bot
uv sync
uv run testplan --help
```

**Update later**: `uv tool upgrade testplan`

### Configuration

**Interactive setup** (recommended):
```bash
testplan setup
```

**Import from .env file**:
```bash
testplan config import .env
```

**Or use environment variables** (for CI/CD):
```bash
export JIRA_URL="https://your-company.atlassian.net"
export JIRA_USERNAME="your-email@company.com"
export JIRA_API_TOKEN="your-token"
export ANTHROPIC_API_KEY="sk-ant-api03-..."
export GITHUB_TOKEN="ghp_..."  # optional
export FIGMA_TOKEN="figd_..."  # optional
```

Config is stored at `~/.config/jira-testplan/config.yaml` with environment variable fallback.

### Usage

```bash
# Check API token health
testplan health

# Generate test plan
testplan generate PROJ-123

# Post directly to Jira
testplan generate PROJ-123 --post-to-jira

# Save to file or copy to clipboard
testplan generate PROJ-123 -o plan.md
testplan generate PROJ-123 --copy

# Batch processing
testplan generate PROJ-123 PROJ-124 PROJ-125

# Output formats: markdown (default), jira, json
testplan generate PROJ-123 --format json
```

### CI/CD Integration

The CLI supports environment variables for automation. Example GitHub Actions workflow:

```yaml
- name: Generate test plan
  run: testplan generate $TICKET --post-to-jira
  env:
    JIRA_URL: ${{ secrets.JIRA_URL }}
    JIRA_USERNAME: ${{ secrets.JIRA_USERNAME }}
    JIRA_API_TOKEN: ${{ secrets.JIRA_API_TOKEN }}
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

## MCP Server - Claude Skill Integration

Use the test plan generator directly within Claude desktop app using natural language.

### Quick Setup

1. **Add to Claude desktop config** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "jira-testplan-bot": {
      "command": "uv",
      "args": ["--directory", "/path/to/jira-testplan-bot", "run", "testplan-mcp"],
      "env": {
        "JIRA_URL": "https://company.atlassian.net",
        "JIRA_USERNAME": "your-email@company.com",
        "JIRA_API_TOKEN": "your-token",
        "ANTHROPIC_API_KEY": "sk-ant-api03-...",
        "GITHUB_TOKEN": "ghp_...",
        "FIGMA_TOKEN": "figd_..."
      }
    }
  }
}
```

2. **Restart Claude desktop**

### Usage

Once configured, use natural language in Claude:
- "Fetch PROJ-123 from Jira"
- "Generate a test plan for PROJ-456"
- "Generate a test plan for PROJ-456 and post it to Jira"
- "Check my API token health"

See [docs/MCP_SERVER.md](docs/MCP_SERVER.md) for detailed setup and troubleshooting.

## Web UI API Endpoints

- **Health check**: `GET /health`
- **API docs**: `/docs` (Swagger UI)
- **Token health**: `GET /health/tokens` - Validates all API tokens
- **Fetch issue**: `GET /issue/{issue_key}` - Returns ticket with development info
- **Generate plan**: `POST /generate-test-plan` - Returns structured test plan JSON

See `/docs` for detailed API documentation and schemas.

## Team Deployment

### Security Requirements

**Current state:** No built-in authentication. For team deployment:
- Add authentication (OAuth, SSO) to protect the application
- Each user should use their own API tokens (never share)
- Use HTTPS in production
- Update CORS settings in `src/app/main.py` for production domains
- Consider using a secrets manager (AWS Secrets Manager, HashiCorp Vault)

### Deployment Options

1. **Personal Use**: Run locally with your own tokens
2. **Internal Team**: Deploy on internal server with SSO + network restrictions
3. **Public SaaS**: Requires multi-tenant architecture, encrypted token storage, and payment integration

### Cost Monitoring

Monitor Claude API usage (pay-per-token). GitHub API has rate limits (5,000/hour). Jira and Figma are typically included with subscriptions.

## Secrets Management

- Never commit `.env` - it's in `.gitignore`
- Use `.env.example` as a template
- Rotate tokens before making the repository public
- For production, use a secrets manager (AWS Secrets Manager, Vault, etc.)

## Testing

```bash
# Unit tests
uv run python tests/run_tests.py

# LLM integration
uv run python tests/test_llm.py

# Full test suite (optional)
uv run pytest tests/ -v
```

## Status

**Current:** Phase 7 complete (Linked ticket dependencies for dependency context)
**Next:** Slack bot integration (Phase 3c)

## Roadmap

### Completed
- ✅ Jira integration with development activity tracking
- ✅ GitHub PR code diffs, comments, and repository docs
- ✅ Figma design context integration
- ✅ Token health monitoring
- ✅ Smart comment management in Jira
- ✅ Priority-based test ordering
- ✅ **Parent ticket context**: Sub-tasks now include parent Epic/Story context with design resources
- ✅ **Linked ticket dependencies**: Automatically fetches blocking/blocked-by relationships for dependency-aware testing

### Future Enhancements
- **Slack Bot**: `/testplan TICKET-123` command for quick generation
- **Screenshot Analysis**: Claude vision API for UI mockup testing
- **Test Plan History**: Save and compare previous generations
- **Quality Feedback**: Thumbs up/down to improve prompts
- **Test Tool Integration**: TestRail, Zephyr, etc.
- **Custom Templates**: Per-team or per-project prompt templates

## Usage Tips

- **Automatic generation**: Just enter a ticket key - the system fetches all context automatically
- **Sub-tasks get parent context**: Design specs (Figma, images) from parent Epics/Stories are automatically included
- **Export formats**: Use Jira format for comments, Markdown for GitHub/Slack, JSON for programmatic use
- **GitHub token recommended**: Adds project-specific terminology and implementation details to test plans

## Parent Ticket Context (Phase 6)

When generating test plans for sub-tasks, the system automatically fetches context from the parent Epic or Story. This is especially valuable because design resources are often attached to parent tickets rather than individual sub-tasks.

### What Gets Fetched from Parent Tickets

- **Parent description**: Business requirements and acceptance criteria
- **Figma designs**: Design specifications linked in parent descriptions
- **Image attachments**: Mockups, screenshots, and design images attached to parent
- **Parent metadata**: Issue type, labels, and summary for broader context

### How It Works

1. System detects if ticket is a sub-task with a parent
2. Fetches full parent ticket data (one additional API call)
3. Extracts Figma URLs from parent description
4. Downloads Figma design context if available
5. Includes parent images (up to 2 from parent, 2 from sub-task = max 4 total)
6. LLM receives both sub-task AND parent context

### Benefits

- **Better context**: Sub-tasks tested with full feature understanding
- **Design access**: Parent-level Figma links and mockups now available
- **Business alignment**: Test plans validate parent-level requirements
- **No extra config**: Works automatically when parent exists

### Example

**Without Parent Context:**
- Sub-task: "Add email validation to form"
- Test plan: Only validates technical implementation

**With Parent Context:**
- Sub-task: "Add email validation to form"
- Parent: "Redesign registration flow" (with Figma designs)
- Test plan: Validates technical implementation AND design requirements from parent Figma

## Linked Ticket Dependencies (Phase 7)

When generating test plans, the system automatically fetches and analyzes linked tickets to understand dependencies. This provides horizontal dependency context to complement the vertical parent hierarchy.

### What Link Types Are Fetched

The system focuses on high-value link types that directly impact testing:

1. **"Blocks"**: Issues this ticket blocks
   - Downstream work depends on this being correct
   - Test thoroughly to prevent breaking dependent tickets

2. **"Is Blocked By"**: Issues blocking this ticket
   - Prerequisites that must be resolved first
   - Understand API contracts and dependencies

3. **"Causes"**: Issues this ticket may cause
   - Validate fixes don't introduce regressions
   - Test related areas carefully

4. **"Is Caused By"**: Root cause issues
   - Ensure the actual cause is fixed, not just symptoms
   - Particularly valuable for bug tickets

### How It Works

1. System fetches issue links from Jira API
2. Parses link types and directions (inward vs outward)
3. Filters for relevant link types (blocks, causes)
4. Fetches basic details for each linked issue (max 5 per type)
5. LLM receives linked context with clear relationship labels

### Benefits

- **Dependency awareness**: Know what must be done first
- **Impact analysis**: Understand what depends on this work
- **Better prioritization**: Test critical paths more thoroughly
- **Root cause validation**: Ensure bugs are truly fixed
- **Regression prevention**: Validate fixes don't break related tickets

### Example

**Scenario:**
- PROJ-101: "Implement Stripe integration" (Status: Done)
- PROJ-102: "Add payment UI" (blocked by PROJ-101)

**When testing PROJ-102:**
- System detects "blocked by PROJ-101"
- Fetches PROJ-101 details (API endpoints, data models)
- LLM generates test plan that validates integration with PROJ-101's API
- Test plan includes prerequisites: "Verify PROJ-101 Stripe API is available"

**Another Scenario:**
- Bug PROJ-200: "Login fails on mobile"
- Root cause: PROJ-150 "Session timeout too short"

**When testing the fix:**
- System detects "caused by PROJ-150"
- Fetches root cause context
- Test plan validates both symptom AND root cause are fixed
- Includes test: "Verify session timeout increased (root cause from PROJ-150)"

## Documentation

See [`docs/PROMPT_IMPROVEMENTS.md`](docs/PROMPT_IMPROVEMENTS.md) for LLM prompt engineering details.

## License

See [LICENSE](LICENSE).
