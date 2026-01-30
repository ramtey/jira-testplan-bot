"""CLI commands."""

from .config_cmd import app as config_app
from .fetch_cmd import fetch
from .generate_cmd import generate
from .health_cmd import health
from .setup_cmd import setup

__all__ = ["config_app", "health", "fetch", "generate", "setup"]
