from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Jira
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""

    # LLM configuration
    llm_provider: str = "claude"  # "claude" (recommended) or "ollama"
    llm_model: str = "claude-3-5-sonnet-20241022"  # For Claude: claude-3-5-sonnet-20241022, claude-3-7-sonnet-20250219; For Ollama: llama3.1, qwen2.5, etc.
    anthropic_api_key: str | None = None  # For Claude API (required when using Claude)
    ollama_base_url: str = "http://localhost:11434"  # Ollama server URL (only needed if using Ollama)

    # App
    app_env: str = "local"


settings = Settings()

# Issue types that don't require test plans
NON_TESTABLE_ISSUE_TYPES = {
    "Epic",
    "Spike",
    "Sub-task",  # Optional - uncomment if sub-tasks shouldn't have test plans
}
