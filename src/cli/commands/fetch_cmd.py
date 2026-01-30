"""Fetch command for retrieving Jira ticket details."""

import asyncio

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from typing_extensions import Annotated

from ..cli_config import config_manager
from ...app.jira_client import (
    JiraAuthError,
    JiraClient,
    JiraConnectionError,
    JiraNotFoundError,
)

console = Console()


def fetch(
    ticket_key: Annotated[str, typer.Argument(help="Jira ticket key (e.g., PROJ-123)")],
):
    """
    Fetch and display Jira ticket details.

    Shows:
        - Ticket summary and description
        - Labels and issue type
        - Development activity (commits, PRs, branches)

    Example:
        testplan fetch PROJ-123
    """
    # Check if configuration exists
    if not config_manager.is_configured():
        console.print("[red]✗ Configuration incomplete![/red]")
        console.print(
            "\nRun [cyan]testplan config set[/cyan] to configure your API tokens."
        )
        raise typer.Exit(1)

    # Load configuration
    config = config_manager.load()

    # Set environment variables for Jira client
    import os

    os.environ["JIRA_BASE_URL"] = config.jira_url or ""
    os.environ["JIRA_EMAIL"] = config.jira_email or ""
    os.environ["JIRA_API_TOKEN"] = config.jira_token or ""

    # Fetch ticket
    with console.status(
        f"[bold blue]Fetching ticket {ticket_key}...", spinner="dots"
    ):
        try:
            jira_client = JiraClient()
            issue = asyncio.run(jira_client.get_issue(ticket_key))
        except JiraNotFoundError:
            console.print(f"[red]✗ Ticket not found:[/red] {ticket_key}")
            raise typer.Exit(1)
        except JiraAuthError as e:
            console.print(f"[red]✗ Jira authentication failed:[/red] {e}")
            console.print(
                "\nCheck your credentials with: [cyan]testplan config show[/cyan]"
            )
            raise typer.Exit(1)
        except JiraConnectionError as e:
            console.print(f"[red]✗ Connection error:[/red] {e}")
            raise typer.Exit(1)

    # Display ticket information
    console.print()
    console.print(
        Panel(
            f"[bold cyan]{issue.key}[/bold cyan]: {issue.summary}",
            title=f"[bold]{issue.issue_type}[/bold]",
            border_style="blue",
        )
    )

    # Display labels if present
    if issue.labels:
        console.print(f"\n[bold]Labels:[/bold] {', '.join(issue.labels)}")

    # Display description
    if issue.description:
        console.print("\n[bold]Description:[/bold]")
        # Truncate very long descriptions
        description = issue.description
        if len(description) > 1000:
            description = description[:1000] + "\n... (truncated)"
        console.print(Panel(description, border_style="dim"))
    else:
        console.print("\n[yellow]No description available[/yellow]")

    # Display development info
    if issue.development_info:
        dev_info = issue.development_info

        # PRs
        if dev_info.pull_requests:
            console.print(f"\n[bold]Pull Requests ({len(dev_info.pull_requests)}):[/bold]")
            pr_table = Table(show_header=True, box=None)
            pr_table.add_column("Status", style="cyan")
            pr_table.add_column("Title")
            pr_table.add_column("Branch", style="dim")

            for pr in dev_info.pull_requests[:5]:  # Show first 5 PRs
                status_color = "green" if pr.status == "MERGED" else "yellow"
                pr_table.add_row(
                    f"[{status_color}]{pr.status}[/{status_color}]",
                    pr.title[:60] + "..." if len(pr.title) > 60 else pr.title,
                    pr.source_branch or "",
                )

            console.print(pr_table)

        # Commits
        if dev_info.commits:
            console.print(f"\n[bold]Commits ({len(dev_info.commits)}):[/bold]")
            for commit in dev_info.commits[:5]:  # Show first 5 commits
                commit_msg = commit.message.split("\n")[0]  # First line only
                if len(commit_msg) > 70:
                    commit_msg = commit_msg[:70] + "..."
                console.print(f"  • {commit_msg}")
                console.print(f"    [dim]{commit.author} - {commit.date}[/dim]")

        # Branches
        if dev_info.branches:
            console.print(f"\n[bold]Branches:[/bold] {', '.join(dev_info.branches[:3])}")

    console.print(
        f"\n[dim]View in Jira: {config.jira_url}/browse/{issue.key}[/dim]"
    )
