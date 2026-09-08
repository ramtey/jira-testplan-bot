"""Watch command — pre-generate plans for tickets landing in the QA queue."""

import asyncio
import os
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table
from typing_extensions import Annotated

from ..cli_config import config_manager
from ...app.config import settings
from ...app.services import queue_watcher

console = Console()

# Colour per outcome so a long sweep reads at a glance.
_ACTION_STYLE = {
    "generated": "green",
    "skipped": "dim",
    "failed": "red",
}

# Skip reasons in the operator's words. A sweep that skips everything should
# say why in terms of the guard that fired, not an enum name.
_REASON_LABEL = {
    queue_watcher.SKIP_ALREADY_PLANNED: "already has a plan",
    queue_watcher.SKIP_COOLDOWN: f"attempted in the last {settings.watch_retry_cooldown_hours}h",
    queue_watcher.SKIP_NO_MERGED_PR: "no merged PR — the diff is still moving",
    queue_watcher.SKIP_ON_HOLD: "on hold — QA parked this ticket",
    queue_watcher.SKIP_CAP_REACHED: "per-sweep cap reached",
    queue_watcher.SKIP_NON_TESTABLE: "issue type isn't planned for",
    "dry_run": "would generate",
    "fetch_failed": "Jira fetch failed",
    "generate_failed": "generation failed",
}


def _apply_config_env() -> None:
    """Push the stored CLI config into the env the app settings read.

    Same shape as `health` and `generate` — the watcher runs outside the
    API process, so it has no other source for these.
    """
    config = config_manager.load()
    os.environ["JIRA_URL"] = config.jira_url or ""
    os.environ["JIRA_USERNAME"] = config.jira_email or ""
    os.environ["JIRA_API_TOKEN"] = config.jira_token or ""
    os.environ["ANTHROPIC_API_KEY"] = config.anthropic_key or ""
    os.environ["GITHUB_TOKEN"] = config.github_token or ""
    os.environ["FIGMA_TOKEN"] = config.figma_token or ""
    os.environ["LLM_PROVIDER"] = "claude"


def _render(result: queue_watcher.SweepResult, *, quiet: bool) -> None:
    if result.queue_error:
        # An unreadable board is not an empty board — never let it read as
        # a quiet success.
        console.print(f"[red]✗ Could not read the queue:[/red] {result.queue_error}")

    if quiet:
        for outcome in result.generated:
            print(outcome.ticket_key)
        return

    if not result.outcomes:
        console.print(
            f"[dim]Queue empty — nothing in \"{settings.watch_status}\".[/dim]"
        )
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Ticket", style="cyan", no_wrap=True)
    table.add_column("Action")
    table.add_column("Detail", style="dim")

    for outcome in result.outcomes:
        label = _REASON_LABEL.get(outcome.reason, outcome.reason or "")
        detail = " — ".join(p for p in (label, outcome.detail) if p)
        table.add_row(
            outcome.ticket_key,
            f"[{_ACTION_STYLE.get(outcome.action, 'white')}]{outcome.action}[/]",
            detail,
        )

    console.print(table)
    console.print(
        f"[dim]{result.scanned} scanned · "
        f"{len(result.generated)} generated · "
        f"{len(result.skipped)} skipped · "
        f"{len(result.failed)} failed[/dim]"
    )


def watch(
    once: Annotated[
        bool,
        typer.Option("--once", help="Run a single sweep and exit instead of looping."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Run every guard but stop short of generating — shows what would be picked up.",
        ),
    ] = False,
    project: Annotated[
        Optional[List[str]],
        typer.Option(
            "--project",
            "-p",
            help="Jira project key to watch (repeatable). Defaults to the configured projects.",
        ),
    ] = None,
    status: Annotated[
        Optional[str],
        typer.Option(
            "--status",
            "-s",
            help=f'Status that means "in the QA queue". Default: "{settings.watch_status}".',
        ),
    ] = None,
    interval: Annotated[
        Optional[int],
        typer.Option(
            "--interval",
            "-i",
            help=f"Seconds between sweeps. Default: {settings.watch_interval_seconds}.",
        ),
    ] = None,
    max_per_sweep: Annotated[
        Optional[int],
        typer.Option(
            "--max",
            help=f"Cap on plans generated per sweep. Default: {settings.watch_max_per_cycle}.",
        ),
    ] = None,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Print only the keys that got a plan."),
    ] = False,
):
    """
    Pre-generate test plans for tickets sitting in the QA queue.

    Sweeps the configured projects for tickets in the queue status and
    generates a plan for any that don't have one — so the tester opens a
    ticket that already has its plan, critics and (for bugs) Bug Lens
    analysis waiting, instead of starting a multi-minute Opus run by hand.

    Each sweep generates real Opus calls with nobody watching, so tickets
    are skipped when they already have a plan, have no merged PR, are on
    hold, were attempted recently, or when the per-sweep cap is hit. Start
    with --dry-run to see what a real sweep would pick up.

    Examples:
        testplan watch --dry-run --once
        testplan watch --once
        testplan watch --interval 600
        testplan watch -p SK -p SL --status "Ready for QA"
    """
    if not config_manager.is_configured():
        console.print("[red]✗ Configuration incomplete![/red]")
        console.print(
            "\nRun [cyan]testplan config show[/cyan] to see missing configuration."
        )
        raise typer.Exit(1)

    _apply_config_env()

    projects = [p.upper() for p in project] if project else queue_watcher.watched_projects()
    status_name = status or settings.watch_status

    if not projects:
        console.print(
            "[red]✗ No projects to watch.[/red] Pass --project or set WATCH_PROJECTS."
        )
        raise typer.Exit(1)

    if not quiet:
        mode = "dry run" if dry_run else "live"
        console.print(
            f"[bold blue]Watching[/bold blue] {', '.join(projects)} "
            f'for tickets in "{status_name}" [dim]({mode})[/dim]'
        )

    if once:
        result = asyncio.run(
            queue_watcher.sweep_once(
                projects=projects,
                status_name=status_name,
                dry_run=dry_run,
                max_per_cycle=max_per_sweep,
            )
        )
        _render(result, quiet=quiet)
        # A queue we couldn't read is a failed sweep — exit non-zero so a
        # cron/launchd wrapper notices instead of logging a silent success.
        if result.queue_error:
            raise typer.Exit(1)
        return

    every = interval or settings.watch_interval_seconds
    if not quiet:
        console.print(f"[dim]Sweeping every {every}s. Ctrl-C to stop.[/dim]\n")

    try:
        asyncio.run(
            queue_watcher.watch(
                projects=projects,
                status_name=status_name,
                interval_seconds=every,
                dry_run=dry_run,
                on_result=lambda r: _render(r, quiet=quiet),
            )
        )
    except KeyboardInterrupt:
        if not quiet:
            console.print("\n[dim]Stopped.[/dim]")
