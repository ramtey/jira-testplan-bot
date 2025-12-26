from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Jira
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""

    # LLM configuration
    llm_provider: str = "ollama"  # "ollama" or "claude"
    llm_model: str = "llama3.1"  # For Ollama: llama3.1, qwen2.5, etc. For Claude: claude-3-5-sonnet-20241022
    ollama_base_url: str = "http://localhost:11434"  # Ollama server URL
    anthropic_api_key: str | None = None  # For Claude API (when available)

    # App
    app_env: str = "local"


settings = Settings()
