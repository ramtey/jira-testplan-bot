"""Main CLI entry point for testplan command."""

import typer
from rich.console import Console
from typing_extensions import Annotated

from . import commands

# Create the main Typer app
app = typer.Typer(
    name="testplan",
    help="Generate structured QA test plans from Jira tickets using AI",
    no_args_is_help=True,
    add_completion=False,
)

# Global console for rich output
console = Console()


# Add version callback
def version_callback(value: bool):
    """Show version and exit."""
    if value:
        console.print("[bold blue]testplan[/bold blue] version [green]0.1.0[/green]")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-v",
            help="Show version and exit",
            callback=version_callback,
            is_eager=True,
        ),
    ] = False,
):
    """
    Generate structured QA test plans from Jira tickets.

    Uses ticket details, development activity (commits, PRs), code changes,
    and repository context to automatically generate comprehensive test plans.
    """
    pass


# Register command groups
app.add_typer(commands.config_app, name="config", help="Manage configuration")
app.command(name="setup")(commands.setup)
app.command(name="health")(commands.health)
app.command(name="fetch")(commands.fetch)
app.command(name="generate")(commands.generate)


if __name__ == "__main__":
    app()
