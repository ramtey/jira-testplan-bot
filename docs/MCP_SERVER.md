# MCP Server - Claude Skill Integration

Use the test plan generator directly within Claude desktop app (or any MCP-compatible client) as a skill.

## What is MCP?

[Model Context Protocol (MCP)](https://modelcontextprotocol.io) is an open protocol that allows AI assistants like Claude to connect to external tools and data sources. This MCP server exposes test plan generation functionality to Claude.

## Available Tools

Once configured, you can use these commands in Claude:

### 1. `fetch_jira_ticket`
Fetch ticket details with development activity.

**Example usage:**
- "Fetch PROJ-123 from Jira"
- "Show me ticket ABC-456"
- "Get details for PROJ-789"

**Returns:** Ticket summary, description, labels, PRs, commits, and branches

### 2. `generate_test_plan`
Generate comprehensive test plan for a ticket.

**Example usage:**
- "Generate a test plan for PROJ-123"
- "Create test cases for ABC-456"
- "Generate tests for PROJ-789"

**Returns:** Structured test plan with happy path, edge cases, and regression checklist

### 3. `check_token_health`
Check health status of all API tokens.

**Example usage:**
- "Check my API token health"
- "Validate my Jira credentials"
- "Are my API tokens working?"

**Returns:** Status of Jira, Claude, GitHub, and Figma tokens

## Installation

### Step 1: Install the Package

```bash
# From the repository directory
cd jira-testplan-bot
uv sync
```

### Step 2: Configure Claude Desktop

1. **Find your Claude desktop config file:**

   - **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

2. **Edit the config file** and add the MCP server:

```json
{
  "mcpServers": {
    "jira-testplan-bot": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/jira-testplan-bot",
        "run",
        "testplan-mcp"
      ],
      "env": {
        "JIRA_BASE_URL": "https://your-company.atlassian.net",
        "JIRA_EMAIL": "your-email@company.com",
        "JIRA_API_TOKEN": "your-jira-api-token",
        "ANTHROPIC_API_KEY": "sk-ant-api03-your-anthropic-key",
        "GITHUB_TOKEN": "ghp_your-github-token",
        "FIGMA_TOKEN": "figd_your-figma-token",
        "LLM_PROVIDER": "claude",
        "LLM_MODEL": "claude-opus-4-5-20251101"
      }
    }
  }
}
```

**Important:**
- Replace `/absolute/path/to/jira-testplan-bot` with the full path to your repository
- Replace all API tokens with your actual credentials
- `GITHUB_TOKEN` and `FIGMA_TOKEN` are optional

3. **Restart Claude desktop** to load the new configuration

### Step 3: Verify Installation

Open Claude desktop and ask:
```
Check my API token health
```

If configured correctly, Claude will use the `check_token_health` tool and show the status of your API tokens.

## Usage Examples

Once installed, you can interact with your Jira tickets naturally:

**Fetch a ticket:**
```
Show me Jira ticket PROJ-123
```

**Generate test plan:**
```
Generate a comprehensive test plan for PROJ-456
```

**Check configuration:**
```
Are my Jira and Claude API tokens working?
```

**Complex workflow:**
```
I need to test ticket ABC-789. First fetch the ticket details,
then generate a test plan focusing on edge cases.
```

## Troubleshooting

### "Tool not found" or "No tools available"

**Solution:** Check your Claude desktop config:
1. Verify the path to `jira-testplan-bot` is correct and absolute
2. Ensure environment variables are set correctly
3. Restart Claude desktop after making changes

### "JIRA_BASE_URL not set" error

**Solution:** Make sure all required environment variables are in the config:
- `JIRA_BASE_URL`
- `JIRA_EMAIL`
- `JIRA_API_TOKEN`
- `ANTHROPIC_API_KEY`

### "Authentication failed" errors

**Solution:**
1. Use `check_token_health` tool to verify which tokens are invalid
2. Get new API tokens if needed:
   - Jira: https://id.atlassian.com/manage-profile/security/api-tokens
   - Anthropic: https://console.anthropic.com/settings/keys
   - GitHub: https://github.com/settings/tokens
3. Update your Claude desktop config with new tokens
4. Restart Claude desktop

### Server crashes or doesn't start

**Solution:**
1. Test the server manually:
   ```bash
   cd jira-testplan-bot
   uv run testplan-mcp
   ```
2. Check for error messages about missing environment variables
3. Verify `uv` is installed: `uv --version`
4. Check Claude desktop logs (see below)

### Viewing Claude Desktop Logs

**macOS:**
```bash
tail -f ~/Library/Logs/Claude/mcp*.log
```

**Windows:**
```powershell
Get-Content -Path "$env:APPDATA\Claude\logs\mcp*.log" -Wait
```

## Configuration Options

### Required Environment Variables

- `JIRA_BASE_URL`: Your Jira instance URL (e.g., https://company.atlassian.net)
- `JIRA_EMAIL`: Your Jira account email
- `JIRA_API_TOKEN`: Jira API token
- `ANTHROPIC_API_KEY`: Anthropic API key for Claude

### Optional Environment Variables

- `GITHUB_TOKEN`: GitHub Personal Access Token (enables PR code diffs and docs)
- `FIGMA_TOKEN`: Figma Personal Access Token (enables design context)
- `LLM_PROVIDER`: Default is "claude" (can also use "ollama")
- `LLM_MODEL`: Default is "claude-opus-4-5-20251101"

## Security Notes

- Your API tokens are stored in Claude desktop config file
- The config file should have restricted permissions (not world-readable)
- Never commit `claude_desktop_config.json` with real credentials to version control
- Use the provided `claude_desktop_config.json` as a template only

## Comparison: MCP Server vs CLI vs Web UI

| Feature | MCP Server (Claude Skill) | CLI | Web UI |
|---------|---------------------------|-----|--------|
| **Use Case** | Natural language interaction | Terminal workflows | Visual interface |
| **Installation** | Claude desktop config | `uv tool install` | Local server |
| **Configuration** | JSON config file | `~/.config` or env vars | `.env` file |
| **Usage** | "Generate test plan for PROJ-123" | `testplan generate PROJ-123` | Click "Generate" |
| **Best For** | Ad-hoc queries while working in Claude | Automation, CI/CD, scripting | Detailed review and exploration |

## Next Steps

- **Learn more about MCP**: https://modelcontextprotocol.io
- **Browse MCP servers**: https://github.com/modelcontextprotocol/servers
- **Report issues**: https://github.com/your-org/jira-testplan-bot/issues
