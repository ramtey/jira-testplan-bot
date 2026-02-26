"""Setup wizard command for interactive configuration."""

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from ..cli_config import config_manager

console = Console()


def setup():
    """
    Interactive setup wizard for first-time configuration.

    This wizard will guide you through configuring:
        - Jira credentials (required)
        - Claude API key (required)
        - GitHub token (optional, for enhanced context)
        - Figma token (optional, for design context)

    Example:
        testplan setup
    """
    console.print()
    console.print(
        Panel.fit(
            "[bold blue]Welcome to the testplan CLI setup wizard![/bold blue]\n\n"
            "This will guide you through configuring API tokens.\n"
            "You can skip optional tokens by pressing Enter.",
            border_style="blue",
        )
    )
    console.print()

    # Check if already configured
    if config_manager.is_configured():
        console.print("[yellow]⚠ Configuration already exists.[/yellow]")
        overwrite = Confirm.ask("Do you want to reconfigure?", default=False)
        if not overwrite:
            console.print("\n[dim]Run [cyan]testplan config show[/cyan] to view current configuration.[/dim]")
            raise typer.Exit(0)
        console.print()

    # Load existing config (if any)
    config = config_manager.load()

    # Jira Configuration
    console.print("[bold cyan]1. Jira Configuration[/bold cyan] (Required)")
    console.print("[dim]Get your API token from: https://id.atlassian.com/manage-profile/security/api-tokens[/dim]\n")

    jira_url = Prompt.ask(
        "Jira Base URL",
        default=config.jira_url or "https://your-company.atlassian.net",
    )
    if jira_url == "https://your-company.atlassian.net":
        console.print("[yellow]⚠ Please enter your actual Jira URL[/yellow]")
        jira_url = Prompt.ask("Jira Base URL")

    jira_email = Prompt.ask(
        "Jira Email",
        default=config.jira_email or "",
    )

    jira_token = Prompt.ask(
        "Jira API Token",
        password=True,
        default=config.jira_token or "",
    )

    config.jira_url = jira_url
    config.jira_email = jira_email
    config.jira_token = jira_token

    console.print("[green]✓[/green] Jira credentials configured\n")

    # Claude API Configuration
    console.print("[bold cyan]2. Claude API Configuration[/bold cyan] (Required)")
    console.print("[dim]Get your API key from: https://console.anthropic.com/settings/keys[/dim]\n")

    anthropic_key = Prompt.ask(
        "Anthropic API Key",
        password=True,
        default=config.anthropic_key or "",
    )

    config.anthropic_key = anthropic_key
    console.print("[green]✓[/green] Claude API key configured\n")

    # GitHub Token (Optional)
    console.print("[bold cyan]3. GitHub Token[/bold cyan] (Optional)")
    console.print("[dim]Enables enhanced context from PR code changes and repository docs[/dim]")
    console.print("[dim]Get token from: https://github.com/settings/tokens[/dim]\n")

    if Confirm.ask("Configure GitHub token?", default=bool(config.github_token)):
        github_token = Prompt.ask(
            "GitHub Personal Access Token",
            password=True,
            default=config.github_token or "",
        )
        if github_token:
            config.github_token = github_token
            console.print("[green]✓[/green] GitHub token configured\n")
        else:
            console.print("[dim]Skipped GitHub token[/dim]\n")
    else:
        console.print("[dim]Skipped GitHub token[/dim]\n")

    # Figma Token (Optional)
    console.print("[bold cyan]4. Figma Token[/bold cyan] (Optional)")
    console.print("[dim]Enables design context from Figma files[/dim]")
    console.print("[dim]Get token from: https://www.figma.com/developers/api#access-tokens[/dim]\n")

    if Confirm.ask("Configure Figma token?", default=bool(config.figma_token)):
        figma_token = Prompt.ask(
            "Figma Personal Access Token",
            password=True,
            default=config.figma_token or "",
        )
        if figma_token:
            config.figma_token = figma_token
            console.print("[green]✓[/green] Figma token configured\n")
        else:
            console.print("[dim]Skipped Figma token[/dim]\n")
    else:
        console.print("[dim]Skipped Figma token[/dim]\n")

    # Save configuration
    console.print("[bold blue]Saving configuration...[/bold blue]")
    config_manager.save(config)
    console.print(f"[green]✓[/green] Configuration saved to: {config_manager.config_file}\n")

    # Test configuration
    console.print("[bold blue]Testing API tokens...[/bold blue]")

    # Set environment variables for health check
    import os

    os.environ["JIRA_URL"] = config.jira_url or ""
    os.environ["JIRA_USERNAME"] = config.jira_email or ""
    os.environ["JIRA_API_TOKEN"] = config.jira_token or ""
    os.environ["ANTHROPIC_API_KEY"] = config.anthropic_key or ""
    os.environ["GITHUB_TOKEN"] = config.github_token or ""
    os.environ["FIGMA_TOKEN"] = config.figma_token or ""
    os.environ["LLM_PROVIDER"] = "claude"

    # Run health check
    from ...app.token_service import TokenHealthService

    with console.status("[bold blue]Validating tokens...", spinner="dots"):
        token_service = TokenHealthService()
        token_statuses = asyncio.run(token_service.validate_all_tokens())

    # Show results
    console.print()
    all_required_valid = True
    for status in token_statuses:
        if status.is_valid:
            console.print(f"[green]✓[/green] {status.service_name}: Valid")
        else:
            status_icon = "[red]✗[/red]" if status.is_required else "[yellow]⚠[/yellow]"
            console.print(f"{status_icon} {status.service_name}: {status.error_message}")
            if status.is_required:
                all_required_valid = False

    console.print()

    if all_required_valid:
        console.print(
            Panel.fit(
                "[bold green]✓ Setup complete![/bold green]\n\n"
                "All required services are configured and working.\n\n"
                "[bold]Next steps:[/bold]\n"
                "  • Run [cyan]testplan fetch PROJ-123[/cyan] to test fetching a ticket\n"
                "  • Run [cyan]testplan generate PROJ-123[/cyan] to generate a test plan\n"
                "  • Run [cyan]testplan --help[/cyan] to see all available commands",
                border_style="green",
            )
        )
    else:
        console.print(
            Panel.fit(
                "[bold yellow]⚠ Setup complete with warnings[/bold yellow]\n\n"
                "Some required services have issues. Please check the errors above.\n\n"
                "Run [cyan]testplan health[/cyan] to check token status again.",
                border_style="yellow",
            )
        )
        raise typer.Exit(1)
