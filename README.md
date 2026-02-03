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
- Jira comments with testing discussions and suggested scenarios
- GitHub PR code changes, comments, and repository documentation
- Figma design specifications (when available)
- Repository test patterns and conventions

**Features:**
- Web UI with dark/light theme support
- CLI tool for terminal-native workflows
- MCP server for Claude desktop integration
- Multiple export formats (Markdown, Jira, JSON)
- Token health monitoring and validation
- Post test plans directly to Jira comments

## Key Features

### Intelligent Context Analysis
- **Automatic context gathering**: Fetches ticket details, PRs, commits, code changes, and repository docs
- **Smart comment analysis**: Extracts testing-related Jira comments (test scenarios, edge cases, QA discussions)
- **Figma integration**: Extracts actual UI component names from design files for specific test cases
- **Smart filtering**: Focuses on runtime behavior, ignoring build-time configs (ESLint, TypeScript, etc.)
- **Priority ordering**: Critical tests first, edge cases last

### Development Integration
- **GitHub enrichment**: PR code diffs, review comments, and repository documentation
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

Required: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `ANTHROPIC_API_KEY`
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
export JIRA_BASE_URL="https://your-company.atlassian.net"
export JIRA_EMAIL="your-email@company.com"
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
    JIRA_BASE_URL: ${{ secrets.JIRA_BASE_URL }}
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
        "JIRA_BASE_URL": "https://company.atlassian.net",
        "JIRA_EMAIL": "your-email@company.com",
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

**Current:** Phase 5 complete (Figma integration, GitHub enrichment, token monitoring)
**Next:** Slack bot integration (Phase 3c)

## Roadmap

### Completed
- ✅ Jira integration with development activity tracking
- ✅ GitHub PR code diffs, comments, and repository docs
- ✅ Figma design context integration
- ✅ Token health monitoring
- ✅ Smart comment management in Jira
- ✅ Priority-based test ordering

### Future Enhancements
- **Slack Bot**: `/testplan TICKET-123` command for quick generation
- **Screenshot Analysis**: Claude vision API for UI mockup testing
- **Test Plan History**: Save and compare previous generations
- **Quality Feedback**: Thumbs up/down to improve prompts
- **Test Tool Integration**: TestRail, Zephyr, etc.
- **Custom Templates**: Per-team or per-project prompt templates

## Usage Tips

- **Automatic generation**: Just enter a ticket key - the system fetches all context automatically
- **Export formats**: Use Jira format for comments, Markdown for GitHub/Slack, JSON for programmatic use
- **GitHub token recommended**: Adds project-specific terminology and implementation details to test plans

## Documentation

See [`docs/PROMPT_IMPROVEMENTS.md`](docs/PROMPT_IMPROVEMENTS.md) for LLM prompt engineering details.

## License

See [LICENSE](LICENSE).
