"""auto-post command — what automatic posting did, so the next step is chosen
from numbers rather than impressions.

Three questions it is built to answer:

* Is anything slipping through? Board-drag posts come from the sweep's
  window; a ticket a person had to post by hand while auto-post sat silent is
  the sign of a gap.
* How often is a plan stale by the time it is posted? That decides whether
  regenerating stale plans automatically is worth the spend.
* After a regeneration, does someone always click Update Jira? If so, that
  update should be automatic too.
"""

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone

import typer
from rich.console import Console
from rich.table import Table
from typing_extensions import Annotated

from ...app.db.mongo import get_db
from ...app.repositories import auto_post_repository
from ...app.services import auto_post

console = Console()

_STYLE = {auto_post.POSTED: "green", auto_post.SKIPPED: "dim", auto_post.FAILED: "red"}


async def _load(days: int) -> tuple[list[dict], dict[int, int]]:
    db = get_db()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    events = await auto_post_repository.list_events_since(db, since=since)
    plan_ids = sorted({e["plan_id"] for e in events if e.get("plan_id") is not None})
    versions: dict[int, int] = {}
    if plan_ids:
        cursor = db["generated_plans"].find({"_id": {"$in": plan_ids}}, {"version": 1})
        versions = {d["_id"]: d.get("version") or 1 for d in await cursor.to_list(length=None)}
    return events, versions


def auto_post_status(
    days: Annotated[
        int, typer.Option("--days", "-d", help="How far back to look.")
    ] = 7,
):
    """
    Show what auto-posting did: posts, skips and failures, and manual posts.

    One row per change of a ticket's outcome, not one per sweep, so a ticket
    the watcher re-checked all day appears once per thing that happened to it.

    Examples:
        testplan auto-post
        testplan auto-post --days 30
    """
    try:
        events, versions = asyncio.run(_load(days))
    except Exception as e:
        # Could not look is not "nothing happened".
        console.print(f"[red]✗ Could not read the auto-post history:[/red] {type(e).__name__}: {e}")
        raise typer.Exit(1)

    console.print(f"[bold]Auto-post · last {days} day{'s' if days != 1 else ''}[/bold]")
    if not events:
        console.print("[dim]Nothing recorded. Auto-post history starts 2026-10-16.[/dim]")
        return

    auto = [e for e in events if e.get("trigger") != auto_post.TRIGGER_MANUAL]
    manual = [e for e in events if e.get("trigger") == auto_post.TRIGGER_MANUAL]

    posted = [e for e in auto if e.get("action") == auto_post.POSTED]
    by_trigger = Counter(e.get("trigger") for e in posted)
    console.print(
        f"  Posted automatically: [green]{len(posted)}[/green]"
        + (
            " (" + ", ".join(f"{t.replace('_', ' ')} {n}" for t, n in by_trigger.most_common()) + ")"
            if posted else ""
        )
    )
    for action, label in ((auto_post.SKIPPED, "Skipped"), (auto_post.FAILED, "Failed")):
        reasons = Counter(
            e.get("reason") for e in auto
            if e.get("action") == action and e.get("reason") != auto_post.SKIP_NO_OWN_PLAN
        )
        if reasons:
            colour = "red" if action == auto_post.FAILED else "yellow"
            console.print(
                f"  {label}: [{colour}]{sum(reasons.values())}[/{colour}] ("
                + ", ".join(f"{r} {n}" for r, n in reasons.most_common()) + ")"
            )
    regenerated = [e for e in manual if versions.get(e.get("plan_id"), 1) > 1]
    console.print(
        f"  Posted by hand: {len(manual)}"
        + (f" ({len(regenerated)} of a regenerated version)" if manual else "")
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("When", style="dim", no_wrap=True)
    table.add_column("Ticket", style="cyan", no_wrap=True)
    table.add_column("Via")
    table.add_column("Outcome")
    table.add_column("Detail", style="dim")
    for e in events:
        at = e.get("at")
        when = at.astimezone().strftime("%a %d %b %H:%M") if at else "?"
        action = e.get("action") or "?"
        outcome = f"[{_STYLE.get(action, 'white')}]{action}[/]"
        if e.get("reason"):
            outcome += f" [dim]{e['reason']}[/dim]"
        plan = e.get("plan_id")
        version = versions.get(plan)
        detail = " — ".join(
            p for p in (f"plan {plan} v{version}" if plan and version else None, e.get("detail")) if p
        )
        table.add_row(when, e.get("ticket_key", "?"), (e.get("trigger") or "?").replace("_", " "), outcome, detail)
    console.print(table)
