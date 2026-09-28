# Configuration

All configuration is environment variables, read from `.env` at process start.
Copy [`.env.example`](../.env.example) to `.env` and fill it in.

> `.env` is read once at startup and uvicorn's `--reload` does not watch it.
> **Restart the backend after any change here** — a stale process serves the
> old values.

## Required

| Variable | What it is |
| --- | --- |
| `JIRA_URL` | e.g. `https://your-company.atlassian.net` |
| `JIRA_USERNAME` | The Atlassian account email the bot acts as |
| `JIRA_API_TOKEN` | Token from [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `ANTHROPIC_API_KEY` | Required when `LLM_PROVIDER=claude` (the default) |
| `MONGODB_URI` | Persistence for plan runs, Bug Lens analyses and test progress |

## LLM

**Using Claude API** (recommended):
1. Get API key from [console.anthropic.com](https://console.anthropic.com/)
2. Add to `.env`:
   ```
   LLM_PROVIDER=claude
   # LLM_MODEL=claude-opus-5   # optional — this is the default
   ANTHROPIC_API_KEY=sk-ant-api03-...
   # Optional: raise from the 600s default when generating plans for
   # parents with many subtasks (Opus can spend several minutes at the
   # 16k-token output cap before the read times out).
   # CLAUDE_API_TIMEOUT_SECONDS=900
   ```

**Alternative**: Ollama (local, free) - set `LLM_PROVIDER=ollama` and `LLM_MODEL=llama3.1` in `.env`

## GitHub (optional)

Enables PR code diffs, review comments, and repository documentation for better test plans.

1. Go to [GitHub Settings → Tokens](https://github.com/settings/tokens)
2. Generate new token with `repo` scope
3. Add to `.env`: `GITHUB_TOKEN=ghp_...`
4. **If using enterprise**: Authorize SSO for your organization

Without GitHub token, test plans use only Jira data (basic PR titles and commits).

## Tell it about your team (optional)

Four settings describe whoever is running the bot. They are config rather than
code on purpose: a clone that inherited another team's Jira projects,
coworkers and repos could only ever be wrong for everyone else, and none of it
belongs in a public repo. All four are JSON, all four are optional, and
`.env.example` carries a worked example of each.

| Setting | What it does | Without it |
|---|---|---|
| `WORKFLOW_PROJECT_PREFIXES` | Which Jira projects get the QA workflow buttons (Pull to Testing, Pass to UAT, Fail back) | No workflow buttons anywhere — the hand-off UI stays hidden |
| `TEAM_GITHUB_LOGIN_TO_JIRA` | GitHub login → `[Jira accountId, display name]`, for choosing who a fail-back returns to | Falls back to searching Jira by commit email → profile name → login; misses anyone whose GitHub name differs from their Jira name, or who commits via a GitHub noreply address. Logged once at startup of the first lookup |
| `BOT_DISPLAY_NAMES` | Jira display names of service accounts that must never be left holding a ticket | Only the accountId-based check guards the hand-off |
| `BUG_LENS_REPO_HINTS` | Product keyword regex → repos to code-search when a bug ticket has no linked PR | Bug Lens searches only repos the ticket actually links |

Finding a Jira accountId: it appears in any user object the Jira API returns,
or query `/rest/api/3/user/search?query=<email>` on your instance.

Note that `.env` is read at process start and uvicorn's `--reload` does not
watch it, so restart the backend after changing any of these — a stale process
will serve the old values (including an empty `WORKFLOW_PROJECT_PREFIXES`,
which reads as "the buttons disappeared").

## Feature gates and tuning

Defaults come from [`src/app/config.py`](../src/app/config.py).

| Variable | Default | What it does |
| --- | --- | --- |
| `LLM_MODEL` | `claude-opus-5` | Per-model request quirks are handled in `model_capabilities.py` |
| `CLAUDE_API_TIMEOUT_SECONDS` | `600` | Raise it for parents with many sub-tasks |
| `REQUIRE_SOURCE_GROUNDING` | `true` | Refuse to generate against context the bot could not actually read |
| `CODE_GROUNDING_RECHECK_ENABLED` | `true` | Third-pass critic that searches the repo to confirm or clear warnings |
| `REGRESSION_GROUNDING_CRITIC_ENABLED` | `true` | Grounding pass over the regression checklist |
| `SURFACE_CLASSIFIER_ENABLED` | `false` | Verification-surface anchoring (deliverable classifier + surface critic) |
| `COPY_ONLY_RULE_ENABLED` | `true` | Caps plan size when a diff only changes user-visible strings |
| `JIRA_STORY_POINTS_FIELD` | `customfield_10004` | Which Jira custom field holds story points |
| `FIGMA_TOKEN` | — | Enables design-spec context |
| `SLACK_USER_TOKEN` | — | Resolves Slack links found in ticket context |
| `MONGODB_DB` | — | Database name; the URI's default database is used when unset |

Queue-watcher settings (`WATCH_*`) are documented with the watcher in
[CLI.md](CLI.md#qa-queue-watcher).

## Secrets management

- Never commit `.env` - it's in `.gitignore`
- Use `.env.example` as a template
- Rotate tokens before making the repository public
- For production, use a secrets manager (AWS Secrets Manager, Vault, etc.)
