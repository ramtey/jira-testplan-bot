"""Config command for managing CLI configuration."""

import typer
from rich.console import Console
from rich.table import Table
from typing_extensions import Annotated

from ..cli_config import config_manager

app = typer.Typer(help="Manage CLI configuration")
console = Console()


@app.command(name="set")
def config_set(
    key: Annotated[str, typer.Argument(help="Configuration key to set")],
    value: Annotated[str, typer.Argument(help="Configuration value")],
):
    """
    Set a configuration value.

    Examples:
        testplan config set jira-url "https://company.atlassian.net"
        testplan config set jira-email "user@company.com"
        testplan config set jira-token "your-token"
        testplan config set anthropic-key "sk-ant-api03-..."
        testplan config set github-token "ghp_..."
        testplan config set figma-token "figd_..."
    """
    valid_keys = [
        "jira-url",
        "jira-email",
        "jira-token",
        "anthropic-key",
        "github-token",
        "figma-token",
    ]

    if key not in valid_keys:
        console.print(
            f"[red]Error:[/red] Invalid key '{key}'. Valid keys: {', '.join(valid_keys)}"
        )
        raise typer.Exit(1)

    try:
        config_manager.set(key, value)
        console.print(f"[green]✓[/green] Configuration updated: [cyan]{key}[/cyan]")
    except Exception as e:
        console.print(f"[red]Error:[/red] Failed to set configuration: {e}")
        raise typer.Exit(1)


@app.command(name="get")
def config_get(
    key: Annotated[str, typer.Argument(help="Configuration key to get")],
):
    """
    Get a configuration value.

    Example:
        testplan config get jira-url
    """
    try:
        value = config_manager.get(key)
        if value is None:
            console.print(f"[yellow]Configuration key '{key}' is not set[/yellow]")
        else:
            # Mask sensitive values
            if any(
                sensitive in key
                for sensitive in ["token", "key", "password"]
            ):
                masked_value = value[:8] + "..." if len(value) > 8 else "***"
                console.print(f"[cyan]{key}:[/cyan] {masked_value}")
            else:
                console.print(f"[cyan]{key}:[/cyan] {value}")
    except Exception as e:
        console.print(f"[red]Error:[/red] Failed to get configuration: {e}")
        raise typer.Exit(1)


@app.command(name="show")
def config_show():
    """
    Show all configuration values.

    Example:
        testplan config show
    """
    try:
        config_dict = config_manager.to_dict()

        if not config_dict:
            console.print("[yellow]No configuration found.[/yellow]")
            console.print(
                "\nRun [cyan]testplan config set <key> <value>[/cyan] to configure."
            )
            return

        table = Table(title="Current Configuration", show_header=True)
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="white")
        table.add_column("Status", style="green")

        for key, value in config_dict.items():
            display_key = key.replace("_", "-")

            # Mask sensitive values
            if any(
                sensitive in key
                for sensitive in ["token", "key", "password"]
            ):
                display_value = value[:8] + "..." if len(value) > 8 else "***"
            else:
                display_value = value

            # Determine status
            is_required = key in ["jira_url", "jira_email", "jira_token", "anthropic_key"]
            status = "Required" if is_required else "Optional"

            table.add_row(display_key, display_value, status)

        console.print(table)

        # Check if configuration is complete
        if not config_manager.is_configured():
            console.print(
                "\n[yellow]⚠ Configuration incomplete![/yellow] Required fields:"
            )
            console.print("  - jira-url")
            console.print("  - jira-email")
            console.print("  - jira-token")
            console.print("  - anthropic-key")

    except Exception as e:
        console.print(f"[red]Error:[/red] Failed to show configuration: {e}")
        raise typer.Exit(1)


@app.command(name="unset")
def config_unset(
    key: Annotated[str, typer.Argument(help="Configuration key to unset")],
):
    """
    Unset (remove) a configuration value.

    Example:
        testplan config unset github-token
    """
    try:
        config_manager.unset(key)
        console.print(f"[green]✓[/green] Configuration removed: [cyan]{key}[/cyan]")
    except Exception as e:
        console.print(f"[red]Error:[/red] Failed to unset configuration: {e}")
        raise typer.Exit(1)


@app.command(name="path")
def config_path():
    """
    Show the path to the configuration file.

    Example:
        testplan config path
    """
    console.print(f"Configuration file: [cyan]{config_manager.config_file}[/cyan]")


@app.command(name="import")
def config_import(
    env_file: Annotated[
        str,
        typer.Argument(help="Path to .env file to import (default: .env in current directory)"),
    ] = ".env",
):
    """
    Import configuration from a .env file.

    This is useful when you already have a .env file for the web UI
    and want to reuse the same credentials for the CLI.

    Example:
        testplan config import .env
        testplan config import /path/to/project/.env
    """
    from pathlib import Path

    env_path = Path(env_file).expanduser().resolve()

    try:
        with console.status(f"[bold blue]Importing from {env_path}...", spinner="dots"):
            result = config_manager.import_from_env_file(env_path)

        # Display results
        if result["imported"]:
            console.print(f"\n[green]✓ Imported {len(result['imported'])} configuration values:[/green]")
            for key in result["imported"]:
                console.print(f"  • {key}")

        if result["skipped"]:
            console.print(f"\n[yellow]⚠ Skipped {len(result['skipped'])} empty or placeholder values:[/yellow]")
            for key in result["skipped"]:
                console.print(f"  • {key}")

        if not result["imported"]:
            console.print("\n[yellow]No configuration values were imported.[/yellow]")
            console.print("Make sure your .env file contains valid API tokens.")
        else:
            console.print(f"\n[green]✓ Configuration saved to:[/green] {config_manager.config_file}")
            console.print("\nRun [cyan]testplan config show[/cyan] to verify.")

    except FileNotFoundError:
        console.print(f"[red]✗ File not found:[/red] {env_path}")
        console.print("\nMake sure the path is correct and the file exists.")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]✗ Import failed:[/red] {e}")
        raise typer.Exit(1)
