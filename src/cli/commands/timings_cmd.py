"""timings command — how long plan generation takes, so "is this one slow?"
has an answer that is not a guess from gaps between watcher log headers.

The time is the run's ``latency_ms``: from just before generation starts
(after the Jira fetch) to the plan being saved, so it covers code reading, the
model and every critic. Runs record their trigger from 2026-10-23; older ones
show as "unknown".
"""

import asyncio
from datetime import datetime, timedelta, timezone
from statistics import median

import typer
from rich.console import Console
from rich.table import Table
from typing_extensions import Annotated

from ...app.db.models.run import RunStatus, RunType
from ...app.db.mongo import get_db

console = Console()

_FIELDS = {
    "created_at": 1, "ticket_keys": 1, "status": 1, "latency_ms": 1,
    "trigger": 1, "error_code": 1, "pr_count": 1, "had_figma": 1,
}


async def _load(days: int) -> list[dict]:
    db = get_db()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    cursor = db["runs"].find(
        {
            "run_type": {"$in": [RunType.test_plan.value, RunType.test_plan_multi.value]},
            "created_at": {"$gte": since},
        },
        _FIELDS,
    ).sort("created_at", 1)
    return await cursor.to_list(length=None)


def _fmt(ms: float) -> str:
    seconds = int(round(ms / 1000))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _p90(values: list[int]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))]


def _stats(values: list[int]) -> str:
    return (
        f"median {_fmt(median(values))} · p90 {_fmt(_p90(values))} · "
        f"max {_fmt(max(values))}"
    )


def timings(
    days: Annotated[
        int, typer.Option("--days", "-d", help="How far back to look.")
    ] = 14,
    top: Annotated[
        int, typer.Option("--top", "-n", help="How many of the slowest runs to list.")
    ] = 10,
):
    """
    Show how long test plan generation takes: typical and slowest, per trigger.

    Examples:
        testplan timings
        testplan timings --days 30 --top 20
    """
    try:
        runs = asyncio.run(_load(days))
    except Exception as e:
        # Could not look is not "nothing ran".
        console.print(f"[red]✗ Could not read the run history:[/red] {type(e).__name__}: {e}")
        raise typer.Exit(1)

    console.print(f"[bold]Plan generation time · last {days} day{'s' if days != 1 else ''}[/bold]")
    if not runs:
        console.print("[dim]No test plan runs recorded in this window.[/dim]")
        return

    # A run is written with latency 0 and gets its time when it finishes, so 0
    # means still running or died without recording an outcome. Averaging
    # those in would make generation look instant.
    timed = [r for r in runs if r.get("latency_ms")]
    untimed = len(runs) - len(timed)
    ok = [r for r in timed if r.get("status") == RunStatus.ok.value]

    if ok:
        console.print(f"  Plans generated: {len(ok)} — {_stats([r['latency_ms'] for r in ok])}")
        by_trigger: dict[str, list[int]] = {}
        for r in ok:
            by_trigger.setdefault(r.get("trigger") or "unknown", []).append(r["latency_ms"])
        for trigger, values in sorted(by_trigger.items(), key=lambda kv: -len(kv[1])):
            console.print(f"    {trigger}: {len(values)} — {_stats(values)}")
    else:
        console.print("  Plans generated: 0")

    for status, label, colour in (
        (RunStatus.error.value, "Failed", "red"),
        (RunStatus.skipped.value, "Skipped", "yellow"),
    ):
        values = [r["latency_ms"] for r in timed if r.get("status") == status]
        if values:
            console.print(f"  {label}: [{colour}]{len(values)}[/{colour}] — {_stats(values)}")
    if untimed:
        console.print(
            f"  [yellow]No time recorded: {untimed}[/yellow] "
            "[dim](still running, or stopped before saving an outcome)[/dim]"
        )

    slowest = sorted(timed, key=lambda r: r["latency_ms"], reverse=True)[:top]
    if not slowest:
        return
    table = Table(show_header=True, header_style="bold", title=f"Slowest {len(slowest)}")
    table.add_column("When", style="dim", no_wrap=True)
    table.add_column("Ticket", style="cyan")
    table.add_column("Via")
    table.add_column("Time", justify="right")
    table.add_column("Outcome")
    table.add_column("PRs", justify="right")
    table.add_column("Figma")
    for r in slowest:
        at = r.get("created_at")
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        when = at.astimezone().strftime("%a %d %b %H:%M") if at else "?"
        status = r.get("status") or "?"
        outcome = {"ok": "[green]ok[/green]", "error": "[red]error[/red]"}.get(status, status)
        if r.get("error_code"):
            outcome += f" [dim]{r['error_code']}[/dim]"
        table.add_row(
            when,
            ", ".join(r.get("ticket_keys") or []) or "?",
            r.get("trigger") or "unknown",
            _fmt(r["latency_ms"]),
            outcome,
            str(r.get("pr_count", 0)),
            "yes" if r.get("had_figma") else "",
        )
    console.print(table)
