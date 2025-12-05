from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Jira
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""

    # LLM (later)
    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"

    # App
    app_env: str = "local"


settings = Settings()
