"""Configuration management for CLI."""

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field


class CLIConfig(BaseModel):
    """CLI configuration model."""

    jira_url: Optional[str] = Field(None, description="Jira base URL")
    jira_email: Optional[str] = Field(None, description="Jira email")
    jira_token: Optional[str] = Field(None, description="Jira API token")
    anthropic_key: Optional[str] = Field(None, description="Anthropic API key")
    github_token: Optional[str] = Field(None, description="GitHub Personal Access Token (optional)")
    figma_token: Optional[str] = Field(None, description="Figma API token (optional)")

    class Config:
        """Pydantic config."""

        extra = "ignore"


class ConfigManager:
    """Manages CLI configuration stored in ~/.config/jira-testplan/config.yaml"""

    def __init__(self):
        """Initialize config manager with default config directory."""
        self.config_dir = Path.home() / ".config" / "jira-testplan"
        self.config_file = self.config_dir / "config.yaml"

    def ensure_config_dir(self) -> None:
        """Ensure config directory exists."""
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> CLIConfig:
        """
        Load configuration from file with environment variable fallback.

        Priority order:
        1. Config file values (highest priority)
        2. Environment variables (fallback)
        3. None (if not set anywhere)
        """
        # Load from file first
        if self.config_file.exists():
            with open(self.config_file, "r") as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}

        # Apply environment variable fallback for missing values
        # Map config keys to environment variable names
        env_mapping = {
            "jira_url": "JIRA_BASE_URL",
            "jira_email": "JIRA_EMAIL",
            "jira_token": "JIRA_API_TOKEN",
            "anthropic_key": "ANTHROPIC_API_KEY",
            "github_token": "GITHUB_TOKEN",
            "figma_token": "FIGMA_TOKEN",
        }

        for config_key, env_var in env_mapping.items():
            # Only use env var if config value is not set
            if not data.get(config_key):
                env_value = os.getenv(env_var)
                if env_value:
                    data[config_key] = env_value

        return CLIConfig(**data)

    def save(self, config: CLIConfig) -> None:
        """Save configuration to file."""
        self.ensure_config_dir()

        # Convert to dict and filter out None values
        data = {k: v for k, v in config.model_dump().items() if v is not None}

        with open(self.config_file, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def get(self, key: str) -> Optional[Any]:
        """Get a configuration value."""
        config = self.load()
        return getattr(config, key.replace("-", "_"), None)

    def set(self, key: str, value: str) -> None:
        """Set a configuration value."""
        config = self.load()
        setattr(config, key.replace("-", "_"), value)
        self.save(config)

    def unset(self, key: str) -> None:
        """Unset a configuration value."""
        config = self.load()
        setattr(config, key.replace("-", "_"), None)
        self.save(config)

    def to_dict(self) -> dict:
        """Convert config to dictionary."""
        config = self.load()
        return {k: v for k, v in config.model_dump().items() if v is not None}

    def import_from_env_file(self, env_file_path: Path) -> dict:
        """
        Import configuration from a .env file.

        Returns a dict with import results: {imported: [], skipped: [], errors: []}
        """
        if not env_file_path.exists():
            raise FileNotFoundError(f".env file not found: {env_file_path}")

        # Load current config
        config = self.load()

        # Map environment variable names to config keys
        env_to_config = {
            "JIRA_BASE_URL": "jira_url",
            "JIRA_EMAIL": "jira_email",
            "JIRA_API_TOKEN": "jira_token",
            "ANTHROPIC_API_KEY": "anthropic_key",
            "GITHUB_TOKEN": "github_token",
            "FIGMA_TOKEN": "figma_token",
        }

        imported = []
        skipped = []

        # Parse .env file
        with open(env_file_path, "r") as f:
            for line in f:
                line = line.strip()

                # Skip comments and empty lines
                if not line or line.startswith("#"):
                    continue

                # Parse KEY=VALUE format
                if "=" not in line:
                    continue

                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()

                # Remove quotes if present
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]

                # Check if this is a key we care about
                if key in env_to_config:
                    config_key = env_to_config[key]
                    # Skip if value is empty or placeholder
                    if not value or value in ["your-token-here", "your-key-here", ""]:
                        skipped.append(key)
                        continue

                    # Set the value
                    setattr(config, config_key, value)
                    imported.append(key)

        # Save updated config
        if imported:
            self.save(config)

        return {"imported": imported, "skipped": skipped}

    def is_configured(self) -> bool:
        """Check if minimum required configuration exists."""
        config = self.load()
        return bool(
            config.jira_url
            and config.jira_email
            and config.jira_token
            and config.anthropic_key
        )


# Global config manager instance
config_manager = ConfigManager()
