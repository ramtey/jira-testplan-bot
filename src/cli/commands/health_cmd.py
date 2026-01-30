"""Health command for checking API token status."""

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from ..cli_config import config_manager
from ...app.token_service import TokenHealthService

console = Console()


def health():
    """
    Check health status of all API tokens.

    Validates:
        - Jira API token (required)
        - Claude/Anthropic API key (required)
        - GitHub token (optional)
        - Figma token (optional)

    Example:
        testplan health
    """
    # Check if configuration exists
    if not config_manager.is_configured():
        console.print("[red]✗ Configuration incomplete![/red]")
        console.print(
            "\nRun [cyan]testplan config show[/cyan] to see missing configuration."
        )
        raise typer.Exit(1)

    # Load configuration
    config = config_manager.load()

    # Set environment variables for token service
    import os

    os.environ["JIRA_BASE_URL"] = config.jira_url or ""
    os.environ["JIRA_EMAIL"] = config.jira_email or ""
    os.environ["JIRA_API_TOKEN"] = config.jira_token or ""
    os.environ["ANTHROPIC_API_KEY"] = config.anthropic_key or ""
    os.environ["GITHUB_TOKEN"] = config.github_token or ""
    os.environ["FIGMA_TOKEN"] = config.figma_token or ""
    os.environ["LLM_PROVIDER"] = "claude"

    # Create token service and validate
    with console.status("[bold blue]Checking API tokens...", spinner="dots"):
        token_service = TokenHealthService()
        token_statuses = asyncio.run(token_service.validate_all_tokens())

    # Display results in a table
    table = Table(title="API Token Health Status", show_header=True)
    table.add_column("Service", style="cyan", no_wrap=True)
    table.add_column("Status", style="white")
    table.add_column("Details", style="dim")
    table.add_column("Required", style="yellow")

    overall_health = True

    for status in token_statuses:
        # Status icon
        if status.is_valid:
            status_icon = "[green]✓ Valid[/green]"
        else:
            status_icon = "[red]✗ Invalid[/red]"
            if status.is_required:
                overall_health = False

        # Details
        if status.is_valid and status.details:
            if "user_email" in status.details:
                details = status.details["user_email"]
            elif "user_login" in status.details:
                details = status.details["user_login"]
            elif "user_name" in status.details:
                details = status.details["user_name"]
            else:
                details = "Connected"
        elif status.error_message:
            details = status.error_message[:50] + "..." if len(status.error_message) > 50 else status.error_message
        else:
            details = "Not configured"

        # Required status
        required_text = "Yes" if status.is_required else "No"

        table.add_row(status.service_name, status_icon, details, required_text)

    console.print(table)

    # Overall health summary
    if overall_health:
        console.print("\n[green]✓ All required services are healthy![/green]")
    else:
        console.print("\n[red]✗ Some required services have issues.[/red]")
        console.print(
            "Run [cyan]testplan config show[/cyan] to check your configuration."
        )
        raise typer.Exit(1)
