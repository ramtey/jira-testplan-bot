# jira-testplan-bot

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)
![React](https://img.shields.io/badge/react-18.3-blue.svg)

Generate structured QA test plans from Jira tickets, and analyze bug tickets to explain root cause, fix, and regression tests — using linked development activity (commits, PRs, code changes).

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
- **Multi-ticket mode**: combine 2+ related tickets into one unified test plan (comma-separated input). When the tickets span multiple repositories, the plan switches to cross-project mode and emits integration tests targeting the producer→consumer seam
- **Jira Bug Lens**: analyze bug tickets for root cause, fix complexity, affected flow, and regression tests
- **Test plan history**: previous test plans for a ticket are surfaced as a banner with view-side-by-side and diff-against-previous-version actions
- **Per-AC coverage**: multi-ticket plans extract acceptance criteria from each ticket, tag every test case with the AC IDs it exercises, and surface a per-ticket coverage matrix with uncovered ACs and a hallucinated-ID guard
- **UI grounding flags**: test steps that reference UI elements not present in the PR diff or simulator `testID` reference are tagged so QA can verify wording before running them
- **PII scrub**: real customer/employee names and emails from Jira/PR context are replaced with generic test-account placeholders before the plan is rendered
- **Epic children view**: fetching an Epic lists every child ticket with per-row Generate and Analyze buttons that render results inline beneath the row
- **Plain-language ticket summary**: collapsible section with a lazy-loaded plain-English explanation of what the ticket does
- **Inline UX feedback**: auto-scroll to results, per-test checkmarks, and a viewport-pinned overall + per-section progress bar
- **Per-test `grounded_in` attribution**: every generated test case carries a `grounded_in` list (e.g. `comments:123`, `PR:456`, `Figma:abc`) rendered as small chips under the test; tests with neither AC coverage nor grounded_in entries get an "Untraced" pill (hidden when the ticket has no ACs at all) so reviewers can spot ungrounded claims at a glance
- **Linked Confluence specs**: Confluence URLs in the Jira description or comments are fetched and injected into the LLM prompt as a LINKED SPECS section so quoted requirements come from the actual spec page, not just the ticket body. Best-effort — per-page failures don't block plan generation
- **Live in Jira badge**: Jira posting is update-in-place, so at most one generated version is the one teammates see on the ticket. The plan banner and run-history rows tag that version "Live in Jira" so users don't double-post or wonder which regeneration is current
- **Shareable URLs**: the active ticket key is mirrored into the URL bar via `?key=…`, so every browser tab is a bookmarkable / refresh-safe handle on a ticket (works alongside the existing per-tab sessionStorage)
- **UAT walkthrough**: every plan is tagged with `uat_complexity` and a plain-language "How to test this" summary. Planners can attach a Loom link, drag-and-drop screenshots (uploaded to Jira as attachments), and setup/repro notes that persist across regenerations; images and videos already uploaded to the linked PR are surfaced in the same card. Pass-to-UAT folds the saved walkthrough into the hand-off comment automatically, and nudges once (escapable) when a high-complexity ticket has no walkthrough
- **Covered-by-unit-tests flag**: cases whose behavior an existing unit test already exercises are flagged and moved into a collapsed section, and excluded from the Jira comment by default
- **Shared per-ticket test progress**: per-test checkmarks are persisted server-side so the whole QA team sees the same checked set; `localStorage` remains an offline fallback

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
- **QA/UAT bounce-back history**: Walks the issue changelog for transitions where the ticket reached an advanced state (QA / UAT / Testing / Ready-for-*) and was sent back to To Do, Backlog, Open, Reopened, or In Progress; pairs each bounce with the nearest Jira comment within ±6 hours (slight bonus when authors match) so the PM's reported reason is captured. Surfaced to the LLM as a "PRIOR QA / UAT BOUNCE-BACK HISTORY" section that asks for explicit regression coverage of each prior failure mode
- **Figma integration**: Extracts actual UI component names from design files for specific test cases
- **Smart filtering**: Focuses on runtime behavior, ignoring build-time configs (ESLint, TypeScript, etc.)
- **Priority ordering**: Critical tests first, edge cases last

### Development Integration
- **GitHub enrichment**: PR code diffs (actual source changes injected into LLM context), review comments, and repository documentation
- **Simulator test context**: Automatically pulls testID references and screen guides from `.agents/skills/simulator-testing/references/` in the target repo (when present), so Claude references real UI test IDs in generated test steps
- **Jira development data**: Commits, branches, and PR statuses with clickable links
- **Open-PR handling**: Open (un-merged) PRs are included in the LLM prompt and flagged as open in the UI header so QA can plan coverage for code that hasn't merged yet
- **Token health monitoring**: Real-time validation with expiration warnings

### Test Plan Generation
- **Claude Opus**: Defaults to `claude-opus-4-5-20251101`; Opus 4.7 is supported (the `temperature` parameter is dropped automatically for 4.7 since the API rejects it). Read timeout is configurable via `CLAUDE_API_TIMEOUT_SECONDS` (default 600s) so worst-case parents with many subtasks survive Opus's 16k-token output cap. Transient `529` overload errors from the plain-summary path are retried with exponential backoff so a brief Anthropic capacity blip no longer drops the ticket summary
- **Smart comment management**: Updates existing Jira comments instead of creating duplicates
- **Multiple export formats**: Markdown, Jira-formatted text, or JSON. The markdown export includes superseded ACs and any grounding warnings so reviewers see the same caveats they would in the UI
- **Issue type validation**: Generates plans for Story, Bug, Task, and Sub-task; skips Epics and Spikes (Epics open the children view instead)
- **Epic launcher view**: Fetching an Epic renders its child tickets as a list with per-row Generate (test plan) and Analyze (Bug Lens) buttons; results expand inline so multiple children can be reviewed without navigating away
- **Multi-ticket AC coverage**: For comma-separated multi-ticket plans, ACs are extracted per ticket, fed to the LLM as a coverage matrix, and each test case must tag the AC IDs it covers. The UI shows per-ticket coverage ratios, lists uncovered ACs, and surfaces a red banner if the model invents AC IDs that don't exist. When two tickets disagree on an AC, the newer ticket's version wins and the older AC is marked superseded
- **Cross-project multi-ticket plans**: When the supplied tickets span multiple repositories, a seam extractor walks each PR diff for HTTP routes, events, and in-house imports, intersects exports/calls across repos, and feeds the resulting verified + suspected seams to the LLM so it emits real integration tests at the boundary. Cross-project test cases are badged with a producer → consumer line and the same metadata flows into the markdown export
- **No silent truncation**: Multi-ticket plans detect when Claude hits the max-tokens cap and surface the truncation explicitly instead of returning a partial plan
- **UI element grounding**: Test steps that name a UI element not present in the PR diff or the target repo's simulator `testID` reference are flagged in the rendered plan so QA can sanity-check the wording before running them
- **Sibling API caller awareness**: Prompt asks the model to enumerate sibling code paths that hit the same API surface (so a fix on one ViewModel doesn't ship with an identical buggy sibling), with a grounding warning when the model can't verify them from the diff. Integration-test rule requires assertions to check that request params are both *present and non-empty*, catching empty-string regressions
- **Observability ticket mode**: Logging / alerting / monitoring tickets switch to a QA-runnable test style — Grafana UI inspection, paste-ready LogQL queries against natural traffic, walking every tab of the affected rule, and `[fill in from UI]` placeholders for values the ticket references but doesn't supply. Bans white-box steps QA can't execute (e.g. "deploy the code", "simulate a DB failure")
- **PII protection**: System prompt forbids naming real customers/employees from ticket context as test subjects; a regex pass scrubs any remaining email-shaped strings from the rendered plan as defense-in-depth
- **Boundary & test-layer prompt rules**: Numeric-boundary changes must produce concrete inside/outside example values and matching step text; filtered-collection assertions must check identity, not just cardinality; backend logic coverage is pushed into a dedicated `[Backend]` section instead of inflating UI/voice steps; mobile tickets ban browser-DevTools instructions
- **Sticky header quick actions**: Copy / Download / Post-to-Jira are reachable from the sticky test-plan header without scrolling to the end of the test list

### Jira Browser Side Rail

A collapsible left rail that lets testers find a ticket without typing a key.
Three drill-down panels mirror Jira's own structure: **Projects → Status
columns → Issues**.

- **Status columns**: statuses are grouped by Jira's `statusCategory` (To Do /
  In Progress / Done) so the rail looks the same across projects with custom
  workflows. Statuses outside the three known categories appear under "Other"
  so nothing is silently hidden
- **Backlog muting**: for projects that use Jira sprints, issues that aren't on
  the active sprint come back with `in_active_sprint=False` and render with a
  soft visual treatment plus a small "Backlog" tag, so the column makes it
  clear which work is actually in flight. Kanban projects without a Sprint
  field render normally — the backend probes per project before applying the
  filter
- **Issue type badges**: each issue row shows a small color-coded badge
  (Story / Bug / Task / Spike / Epic / Sub-task) using the same palette as
  the main ticket header
- **Pinned + Recent**: pin frequently-used projects with the star icon — they
  appear in a "Pinned" group at the top of the projects list. Recently visited
  projects auto-populate a "Recent" group below it (capped at 5, excluding
  pinned to avoid duplication). Pins and recents are persisted in
  `localStorage` per browser. Both sections are hidden while the filter input
  is in use
- **Refresh model**: every panel has a manual ↻ button, and the active panel
  silently re-fetches whenever the tab regains visibility (covers the common
  "I just changed something in the Jira tab" case). Silent refresh keeps the
  current data on screen while the request is in flight — no spinner flash
- **Selection**: clicking an issue populates the existing input field and
  triggers the normal fetch flow, so the rail is purely additive; the
  paste-a-key input remains the escape hatch for power users
- **Subtask grouping**: when an actual Sub-task issue type and its parent
  both live in the same status column, the subtask row is hidden and its
  parent gets a small `+N sub` pill. Sub-tasks whose parent is in another
  column still appear, indented with a faint vertical tree-line and a
  `SUBTASK OF KEY` caption so the relationship reads at a glance.
  Stories/Tasks under an Epic are *not* affected — they keep their own row
- **Fetch overlay**: while a ticket is loading the rail and main column are
  covered by a centered overlay + spinner so the user gets clear feedback
  instead of a stuck inline button state
- **Auth note**: the rail surfaces only what the configured `JIRA_USERNAME` /
  `JIRA_API_TOKEN` can see. Project list is capped at the first 100 results
  from `/rest/api/3/project/search`

### QA Workflow Actions

One-click status transitions plus reassignment, to remove the "transition →
pick assignee" two-step from the QA loop. Frontend button visibility is
config-driven via `WORKFLOW_PROJECT_PREFIXES` (default `["SK"]`) — list
additional Jira project keys to surface the QA workflow buttons for those
projects without code changes. The backend endpoint itself still hardcodes
the SK-only check; widening it (e.g. honouring the same setting or a
per-project status map) is the next step before non-SK projects can fully
opt in.

- **Pull to Testing**: shown when the ticket is *not* already in *In Testing*.
  Transitions to *In Testing* and assigns the ticket to the current Jira user
  (the one whose `JIRA_USERNAME` / `JIRA_API_TOKEN` is configured). If the
  ticket has no stored test-plan run and none is loaded in the session, a
  fresh plan is generated automatically — re-pulls and bounce-backs reuse the
  existing plan rather than re-spending on the LLM
- **Pass to UAT**: shown when the ticket is in *In Testing*. Opens an inline
  note form with a "Tested in" chip row (Integ / Staging / Prod multi-select,
  preselected by scanning the latest comment + description for the
  corresponding env name, with selected chips rendered as solid ✓-prefixed
  pills and unselected chips as dashed-border outlines for dark-mode clarity),
  an optional Loom URL textarea (one per line — each is rendered as its own
  paragraph above the fold), an optional screenshot/PDF dropzone (click /
  drag / paste — files upload directly to the Jira issue as attachments
  before the transition runs, and the comment links each by filename), and
  an optional markdown summary. Submitting transitions to *Ready for UAT*,
  reassigns to the dev who handed it over, and posts a Jira comment whose
  marker line (e.g. `✅ QA Passed (Integ + Staging) — ready for UAT`) stays
  visible with the summary tucked into a collapsible expand block. The
  ticket's saved walkthrough (Loom link, screenshots-as-attachments, notes)
  is always folded into the comment too, so "how to test this" travels
  with the transition even when the form was left empty; if the ticket is
  flagged high-complexity and has no walkthrough or attached media, a
  two-step nudge ("add a walkthrough?") appears before submitting — always
  escapable. Submitting the form empty with no saved walkthrough preserves
  the original one-click pass with no comment. If this is the last sibling
  sub-task to reach Ready for UAT (others already passed or Done), the
  parent ticket is auto-promoted to Ready for UAT in the same call (Epics
  excluded; best-effort, won't fail the primary transition)
- **Fail back**: shown when the ticket is in *In Testing*. Renders as a
  single "Fail back to" trigger plus inline **To Do** / **In Progress**
  destination chips in one bordered unit — the trigger commits the bounce
  to whichever chip is selected, so the two same-verb bounce-backs don't
  clutter the toolbar as a wall of red buttons. To Do drops the ticket back
  into the dev backlog; In Progress keeps it in-flight for immediate
  rework. Opens the same inline form pattern as Pass to UAT — a *required*
  Reason field (markdown, autofocused, rendered above the fold so devs see
  *why* without expanding), plus an optional multi-Loom textarea and the
  same screenshot/PDF dropzone (files attached to the issue, linked by
  filename in the comment). Empty submit is rejected because a fail-back
  without a reason has no value. The transition still runs even if the
  comment post fails, matching Pass to UAT. The post-action banner is
  rendered in a warning tone ("Bounced back to …") instead of the
  celebratory green check used for UAT pass, so the bounce-back is
  visually unmistakable
- **Notify chip picker**: Both forms expose an optional Notify row that
  @mentions selected users in the posted comment via a real ADF mention node
  in a trailing `cc:` paragraph (so Jira actually delivers notifications, not
  just text that looks like a tag). Candidates come from people already on
  the ticket: current assignee (starred), prior assignees from the changelog,
  and recent commenters; the configured bot user is filtered out
- **Also move all subtasks**: Workflow forms include an opt-in "Also move all
  subtasks" checkbox (hidden when the ticket has no subtasks). When checked,
  the backend captures the parent's *pre-transition* status, then re-applies
  the target status only to subtasks whose current status matches that
  pre-transition state — so a parent moving out of *Ready to Test* only
  pulls subtasks that were also in *Ready to Test*, leaving siblings in
  unrelated states alone. Subtasks whose workflow has no matching transition
  are skipped silently so a partial workflow doesn't break the primary
  action
- **Assignee fallback chain** (Pass to UAT / Fail back): walks the issue
  changelog for the prior assignee (skipping the bot's own account, since
  Pull to Testing parks the ticket there). If none is found, falls back to
  the top contributor across the ticket's linked PRs (highest
  additions+deletions), mapping GitHub login → Jira account via commit
  author email, then public profile email/name, then login. If neither
  resolves, the ticket is left unassigned and the UI toast says so
- **Available transition guard**: each action looks up the issue's available
  transitions before acting. If the target status isn't reachable from the
  current state the API returns 400 with the list of valid transitions, so
  bad clicks fail loudly instead of silently no-op'ing
- **Endpoint**: `POST /issue/{issue_key}/workflow/{pull-to-testing|pass-to-uat|fail-to-todo|fail-to-in-progress}`

### Jira Bug Lens
Analyze bug tickets to go beyond the ticket description and into the code:
- **Bug summary**: Plain-English explanation of what broke and what the user experienced
- **Root cause**: Identifies the exact cause in the code, referencing specific files and logic (requires a linked PR with diffs)
- **Fix explanation**: Describes what the merged PR changed to resolve the bug
- **Fix complexity estimate**: For unfixed bugs, infers the GitHub repo from the ticket and estimates effort required
- **Affected flow & scope of impact**: Identifies which user flows are broken and how wide the blast radius is
- **Test gap analysis**: Highlights what testing was missing that allowed the bug through
- **Regression tests**: Concrete, actionable test cases to prevent the bug from recurring
- **Similar patterns**: Classes of related bugs to proactively look for in the codebase
- **Code evidence**: Deterministic GitHub code search for LLM-suspected symbols — each analysis lists the exact files, line numbers, and code snippets where the suspects appear, with clickable links. Doc files (`.md`/`.rst`) are filtered and zero-hit suspects are hidden.
- **Multi-ticket support**: Analyze multiple related bug tickets together for a combined root cause analysis
- **Download as .md**: Export the full analysis as a Markdown file
- Only shown for `Bug` issue type; automatically uses the same GitHub PR diff pipeline as test plan generation

### Test Plan History

Every successful test plan run is persisted to Postgres so prior versions stay
recoverable and comparable.

- **Prior-runs banner**: When a ticket has prior successful test-plan runs, a
  banner appears above the generate area summarising the latest version and
  expanding to a list of every version with creation time, model, and case count
- **Live in Jira badge**: Posting a plan to Jira updates the existing
  comment in place, so at most one version is the one teammates actually
  see. That version is tagged "Live in Jira" on the run-history rows and
  on the plan banner so reviewers don't double-post or wonder which
  regeneration is current
- **Side-by-side preview**: Clicking *View* on a row renders the historical
  plan below the live one in muted gray styling, so versions can be read
  side-by-side without losing the active output. The preview is read-only —
  duplicate Post-to-Jira/Copy/Download actions are hidden
- **Version diff**: Clicking *Diff* opens a unified line-diff of the markdown-
  formatted plan against its immediate predecessor (`generated_plans.previous_plan_id`)
- **Auto-chained regenerations**: Single-ticket regenerations automatically set
  `previous_plan_id` and bump `version`, so the chain forms without any user
  action
- The history banner hides while Bug Lens analysis is running or showing, so
  the two flows don't visually overlap; persisting Bug Lens output (with its
  own history banner) is a planned follow-up

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
   # Optional: raise from the 600s default when generating plans for
   # parents with many subtasks (Opus can spend several minutes at the
   # 16k-token output cap before the read times out).
   # CLAUDE_API_TIMEOUT_SECONDS=900
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

Use the test plan generator directly within Claude desktop app using natural language. The MCP path now feeds the LLM the same context as the REST API and CLI (parent ticket, comments, linked issues, image attachments), so sub-tasks generated through Claude Desktop pick up parent Epic/Story Figma designs and descriptions automatically.

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
- **List Epic children**: `GET /issue/{epic_key}/children` - Lightweight list (key, summary, issue_type, status) of tickets directly under an Epic; powers the Epic launcher view
- **Generate plan**: `POST /generate-test-plan` - Returns structured test plan JSON
- **Generate multi-ticket plan**: `POST /generate-test-plan/multi` - Unified plan from 2+ related tickets. Switches between *single_repo* mode (shared repo / overlapping files) and *cross_project* mode (tickets span repos — seams extracted from the PR diffs drive integration-test generation)
- **Analyze bug**: `POST /bug-lens/analyze` - Root cause, fix explanation, and regression tests for a bug ticket
- **Analyze bugs (multi)**: `POST /bug-lens/analyze/multi` - Combined analysis for multiple related bug tickets
- **List runs by ticket**: `GET /runs/by-ticket/{key}` - Successful test-plan runs for a ticket, newest first; powers the history banner
- **Fetch stored plan**: `GET /plans/{plan_id}` - Full plan body and ordered test cases for a stored generation; powers View and Diff
- **QA workflow action**: `POST /issue/{issue_key}/workflow/{action}` - Transition + reassignment (`pull-to-testing`, `pass-to-uat`, `fail-to-todo`, `fail-to-in-progress`); backend still rejects non-`SK-` keys with 400 (frontend visibility is the config-driven layer). Accepts `multipart/form-data` with optional comment fields (envs, `loom_urls` list, summary, reason, screenshot file uploads), `mention_account_ids` for ADF @mentions, and `cascade_to_subtasks` to re-apply the transition to each direct subtask whose status matched the parent's *pre-transition* status
- **Ticket walkthrough**: `GET/PUT /tickets/{ticket_key}/walkthrough` - Human-authored Loom link, screenshots (uploaded to Jira as attachments), and setup/repro notes for the ticket; folded into the Pass-to-UAT comment automatically. GET also returns the latest known `uat_complexity` so the workflow UI knows whether to nudge
- **Test-plan progress**: `GET/PUT /test-plan-progress/{progress_key}` - Shared per-ticket checkmark state (which test cases QA has ticked off), keyed by ticket + plan fingerprint so the whole team converges on the same set

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

**Current:** Per-test `grounded_in` attribution with Untraced flagging, linked Confluence specs feeding the prompt, "Live in Jira" badge on the version that's currently posted, URL deep linking via `?key=…`, rail backlog muting, sticky-header quick actions, and tighter boundary / test-layer prompt rules shipped on top of the cross-project multi-ticket / Opus 4.7 / observability-prompt baseline; prompt quality hardening ongoing

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
- ✅ **PR repo label**: Each PR in Development Activity shows which repo it belongs to (`owner/repo`)
- ✅ **PR author**: Each PR displays the GitHub author login
- ✅ **Assignee history**: All unique people ever assigned to a ticket (from Jira changelog), with current assignee highlighted
- ✅ **Multi-ticket test plans**: Enter comma-separated ticket keys (e.g. `PROJ-123, PROJ-456`) to generate one unified plan; requires shared repo or overlapping file changes
- ✅ **Jira Bug Lens**: Analyze bug tickets to explain root cause, fix, and suggest regression tests; supports multi-ticket analysis
- ✅ **Bug Lens v2**: Fix complexity estimate, affected flow, scope of impact, test gap analysis, download as .md
- ✅ **Bug Lens Code Evidence**: Grep-based grounding section showing where LLM-suspected symbols actually exist in the repo, with clickable links per hit
- ✅ **Plain-language ticket summary**: Lazy-loaded collapsible explanation of what the ticket does
- ✅ **Prompt caching**: Static system prompt cached via Claude API for lower latency and cost
- ✅ **Structured tool-use output**: Claude tool use enforces JSON schema on test plan output (replaces regex parsing)
- ✅ **Formatted Jira comments**: Test plans posted as rich ADF (Atlassian Document Format) instead of plain text
- ✅ **UX polish**: Auto-scroll to results, inline button-state feedback, red ticket badge in Bug Lens
- ✅ **Test plan history**: Persist every test-plan run to Postgres; surface prior versions in a banner with side-by-side view and diff against the previous version; regenerations auto-chain via `previous_plan_id`
- ✅ **Jira browser side rail**: Collapsible Projects → Status → Issues drill-down with status-category grouping, type badges, pinned + recent project shortcuts, and silent refresh on tab focus
- ✅ **QA workflow buttons**: One-click *Pull to Testing* / *Pass to UAT* / *Fail back to To Do* for the SK project, with automatic reassignment (current user on pull, prior assignee on pass/fail)
- ✅ **Sub-task test plans**: Sub-tasks are now a testable issue type and flow through the same generation path as Story/Task/Bug, while still inheriting parent Epic/Story design context
- ✅ **MCP context parity**: MCP `generate_test_plan` mirrors CLI/REST context assembly (parent, comments, linked issues, images), so Claude Desktop sub-task plans no longer miss parent design resources
- ✅ **Workflow assignee fallback**: Pass to UAT / Fail back to To Do fall back from changelog prior-assignee → top PR contributor → unassigned, skipping the bot's own account in the changelog
- ✅ **Hotfix-aware prompt filter**: Open hotfix PRs (title/branch contains `hotfix`) stay in the LLM prompt while other open PRs are excluded
- ✅ **QA/UAT bounce-back awareness**: Changelog walker detects prior QA/UAT failures, pairs each with the nearest Jira comment, and feeds them into the LLM prompt so regenerated plans cover the prior failure modes
- ✅ **Auto test plan after Pull to Testing**: First *Pull to Testing* on a ticket with no stored run kicks off a generation automatically; subsequent re-pulls/bounce-backs reuse the existing plan
- ✅ **Pass-to-UAT note form**: Inline form on Pass to UAT with env chips (Integ/Staging, preselected by scanning the latest comment / description), optional Loom URL, and markdown summary; posts a marker-line + collapsible-block Jira comment, or stays one-click when submitted empty
- ✅ **Fail back to To Do**: Renamed from *Fail back to In Progress* and retargeted at *To Do* so failing QA drops the ticket back into the dev queue
- ✅ **Fail-back form**: Reason (required, autofocused, markdown) + optional Loom + image URL list, mirroring the Pass-to-UAT pattern
- ✅ **Pass-to-UAT Image URLs**: Optional textarea for screenshot links, rendered as clickable 🖼️ entries above the test-summary expand block
- ✅ **Prod env chip**: Pass-to-UAT now offers Integ / Staging / Prod with auto-selection from the ticket text and clearer selected/unselected styling
- ✅ **Notify @mentions**: Optional chip picker on both QA workflow forms emits real ADF mention nodes so Jira sends notifications to selected ticket participants
- ✅ **Cascade to subtasks**: Opt-in "Also move all subtasks" checkbox on workflow actions re-applies the parent's transition to each direct subtask
- ✅ **Parent auto-promotion**: Passing the last sibling sub-task to Ready for UAT also moves the parent (Epics excluded), removing the manual follow-up
- ✅ **Per-AC coverage (multi-ticket)**: Acceptance criteria are extracted per ticket, fed to the LLM as a coverage matrix, and each test case must tag the AC IDs it covers; the UI surfaces ratios, uncovered ACs, and a hallucinated-ID guard
- ✅ **AC conflict resolution**: When two tickets in a multi-ticket plan disagree on the same AC, the newer ticket wins and the older AC is shown as superseded in the UI and markdown export
- ✅ **No-truncation guard**: Multi-ticket plans detect Claude's max-tokens cap and surface truncation instead of silently returning a partial plan
- ✅ **UI element grounding**: Test steps that reference UI elements not present in the PR diff or the target repo's `testID` reference are flagged in the rendered plan
- ✅ **Sibling API caller / non-empty param rules**: Prompt asks for sibling code paths hitting the same API surface, with grounding warnings for unverifiable callers, and integration-test rule requires present-AND-non-empty assertions on request params
- ✅ **Observability ticket reframing**: Logging/alerting/monitoring tickets switch to QA-runnable Grafana-UI steps with paste-ready LogQL queries, full-rule walks, and `[fill in]` placeholders instead of white-box infrastructure tampering
- ✅ **PII scrub**: System-prompt guardrail plus regex pass replaces real customer/employee emails in rendered test plans with `<test-account>` placeholders
- ✅ **Open PRs back in prompt**: Open (un-merged) PRs are again included in the LLM context (still flagged as open in the UI header), replacing the earlier hotfix-only carve-out
- ✅ **Per-test progress UI**: Per-test checkmarks plus a gradient overall + per-section progress bar pinned to the top of the viewport while scrolling
- ✅ **Gaps-only description panel**: Replaced the description-quality metrics + Weak/Good label with a panel that lists concrete gaps (missing AC for stories, missing repro / expected-vs-actual for bugs) and hides itself entirely when nothing is missing
- ✅ **Markdown export parity**: Export includes superseded ACs and any grounding warnings so reviewers see the same caveats they would in the UI
- ✅ **Historical-plan export buttons**: Copy/Download show on the historical plan view when no live plan exists, so reviewers can still get markdown out of an older run
- ✅ **Cross-project multi-ticket plans**: Tickets spanning multiple repos no longer 422; a seam extractor reads each PR diff and feeds verified + suspected producer→consumer seams to the LLM so it emits integration tests at the boundary
- ✅ **Screenshot uploads on QA workflow**: Pass to UAT / Fail back replaced the "Image URLs" textarea with a click/drag/paste dropzone that attaches files to the Jira issue directly (multipart upload before the transition, so a Jira-side failure aborts cleanly)
- ✅ **Multi-Loom on QA workflow**: Pass to UAT / Fail back now accept multiple Loom URLs (one per line); each renders as its own paragraph above the fold of the posted comment
- ✅ **Cascade-to-subtasks pre-transition filter**: The "Also move all subtasks" checkbox now restricts cascade targets to subtasks whose current status matches the parent's pre-transition status, so unrelated siblings aren't dragged along
- ✅ **Workflow project gate via config (frontend)**: Replaced the hardcoded SK-only check in `WorkflowActions` with a `WORKFLOW_PROJECT_PREFIXES` setting (frontend reads it via the existing `/config` endpoint) so additional Jira projects can surface the buttons without code changes; the backend endpoint still gates on `SK-` and is the next step before non-SK projects fully opt in
- ✅ **Rail subtask grouping + fetch overlay**: The Jira browser rail collapses Sub-tasks under their visible parent with a `+N sub` pill, and badges orphan Sub-tasks (parent in another column) with an indented `SUBTASK OF KEY` caption. A full-screen overlay covers the app while a ticket is loading
- ✅ **Workbench frontend redesign**: Frontend refactored around a workbench-style design system; App state extracted into dedicated hooks and TestPlanDisplay sections collapsed into one config-driven map
- ✅ **Opus 4.7 readiness**: Claude calls drop the `temperature` parameter automatically for Opus 4.7 (the API rejects it), and the read timeout is configurable via `CLAUDE_API_TIMEOUT_SECONDS` (default 600s) so worst-case parents survive the 16k-token output cap
- ✅ **URL deep linking**: `?key=…` seeds the fetch on first paint and the active ticket is mirrored back into the URL via `replaceState`, so every browser tab is a bookmarkable / refresh-safe handle on a ticket (and the loaded key now also appears in the browser tab title)
- ✅ **Per-test `grounded_in` attribution**: Each generated test case carries a `grounded_in` list (e.g. `comments:123`, `PR:456`, `Figma:abc`) rendered as chips under the test; tests with neither AC coverage nor grounded_in get an "Untraced" pill (hidden when the ticket has no ACs)
- ✅ **Linked Confluence specs**: Confluence URLs in the Jira description or comments are fetched (reusing Atlassian Cloud Basic auth) and injected into the LLM prompt as a LINKED SPECS section so quoted requirements come from the actual spec page. Best-effort — per-page fetch failures don't block plan generation
- ✅ **Live in Jira badge**: A `posted_to_jira` marker tracks which generated plan is the one currently in the Jira comment (posting is update-in-place, so at most one); the badge is surfaced both on the active plan banner and on every matching run-history row
- ✅ **Sticky-header quick actions**: Copy / Download / Post-to-Jira are reachable from the sticky test-plan header, not just from the end of the test list
- ✅ **Rail backlog muting**: For sprint-using projects, issues outside the active sprint render with a soft visual treatment and a "Backlog" tag; Kanban projects without a Sprint field render normally
- ✅ **Boundary & test-layer prompt rules**: Numeric-boundary changes force inside/outside examples and matching step text; filtered-collection assertions check identity not cardinality; backend logic gets its own `[Backend]` section; mobile tickets ban browser-DevTools instructions
- ✅ **Fail-back distinct from pass banner**: Fail back to To Do now renders a warning-tone "Bounced back to …" banner instead of the green check used for UAT pass, so the bounce-back reads as a return-to-dev rather than progress
- ✅ **Transient 529 retry on summarization**: The plain-summary Claude call retries Anthropic `529` overload errors with exponential backoff so a brief capacity blip doesn't drop the ticket summary
- ✅ **Workflow routes module**: QA workflow endpoint, its constants, and the parent/subtask cascade helpers moved out of `main.py` into a dedicated `workflow_routes.py`, matching `bug_lens_routes` / `runs_routes` (same URLs, same behavior)

### Future Enhancements
- **Screenshot Analysis**: Claude vision API for UI mockup testing
- **Bug Lens history**: Persist and surface prior Bug Lens analyses the same way test plans are surfaced
- **Quality Feedback**: Thumbs up/down to improve prompts
- **Test Tool Integration**: TestRail, Zephyr, etc.
- **Custom Templates**: Per-team or per-project prompt templates

## Usage Tips

- **Automatic generation**: Just enter a ticket key - the system fetches all context automatically
- **Multi-ticket mode**: Enter comma-separated keys (`PROJ-123, PROJ-456`) to combine related tickets into one plan; tickets must share a repository or overlapping changed files
- **Bug Lens**: For `Bug` tickets, an "Analyze Bug" button appears alongside "Generate Test Plan" — use it to get root cause, fix explanation, and regression tests grounded in the actual PR diff
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
