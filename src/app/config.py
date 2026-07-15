from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Jira
    jira_url: str = ""
    jira_username: str = ""
    jira_api_token: str = ""

    # LLM configuration
    llm_provider: str = "claude"  # "claude" (recommended) or "ollama"
    llm_model: str = "claude-opus-4-5-20251101"  # For Claude: claude-opus-4-5-20251101, claude-sonnet-4-5-20250929; For Ollama: llama3.1, qwen2.5, etc.
    anthropic_api_key: str | None = None  # For Claude API (required when using Claude)
    ollama_base_url: str = "http://localhost:11434"  # Ollama server URL (only needed if using Ollama)
    # Read timeout (seconds) for Claude test-plan generation. Large parents
    # (e.g. an Epic + many subtasks) push the prompt big enough that Opus can
    # spend several minutes producing 16k output tokens; 120s would surface as
    # "Claude API request timed out" mid-generation.
    claude_api_timeout_seconds: float = 600.0

    # GitHub (for PR diff fetching - Phase 3a)
    github_token: str | None = None  # GitHub personal access token (optional - enables PR diff fetching)

    # Third-pass critic that re-checks AC-grounding warnings against the linked
    # repo's actual source. When True and a github_token is available, an
    # AC-critic warning whose behaviour is present in code gets downgraded from
    # WARN to INFO so QA doesn't chase a false positive. Set to False to skip
    # the extra GitHub search + LLM round-trip (~2s + ~1s per flagged case).
    code_grounding_recheck_enabled: bool = True

    # Bug Lens repo hints: maps a regex pattern (matched against summary + description + comments)
    # to one or more "owner/repo" strings to search when the ticket has no explicit GitHub links.
    # Set via env as JSON, e.g. BUG_LENS_REPO_HINTS='{"title.?rep|folders": ["skyslope/mobile-app"]}'
    bug_lens_repo_hints: dict[str, list[str]] = {}

    # Figma (for design context - Phase 5)
    figma_token: str | None = None  # Figma personal access token (optional - enables design context)

    # Slack (for resolving Slack message links in Jira descriptions/comments)
    slack_user_token: str | None = None  # Slack user token (xoxp-) - required scopes: channels:history, groups:history, im:history, mpim:history, users:read

    # App
    app_env: str = "local"

    # QA workflow buttons (Pull-to-Testing / Pass-to-UAT / Fail-back) only show
    # for tickets whose key starts with one of these prefixes. Empty list
    # disables the workflow UI entirely. Set via env as JSON, e.g.
    # WORKFLOW_PROJECT_PREFIXES='["SK","SL"]'.
    workflow_project_prefixes: list[str] = ["SK"]

    # Database (Neon Postgres)
    database_url: str | None = None


settings = Settings()

# Issue types that don't require test plans
NON_TESTABLE_ISSUE_TYPES = {
    "Epic",
    "Spike",
}
