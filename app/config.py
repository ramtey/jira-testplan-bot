from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Jira Configuration
    jira_url: str
    jira_email: str
    jira_api_token: str
    
    # LLM Configuration
    openai_api_key: str
    openai_model: str = "gpt-4"
    
    # Application Configuration
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    
    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
