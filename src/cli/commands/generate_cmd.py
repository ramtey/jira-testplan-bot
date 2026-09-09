"""Generate command for creating test plans."""

import asyncio
import json
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.syntax import Syntax
from typing_extensions import Annotated

from ..cli_config import config_manager
from ...app.jira_client import (
    JiraAuthError,
    JiraClient,
    JiraConnectionError,
    JiraNotFoundError,
)
from ...app.llm_client import LLMError
from ...app.models import GenerateTestPlanRequest
from ...app.services import plan_service
from ...app.services.plan_service import NonTestableIssueError

console = Console()


def generate(
    ticket_keys: Annotated[
        List[str], typer.Argument(help="Jira ticket key(s) (e.g., PROJ-123)")
    ],
    output: Annotated[
        Optional[Path],
        typer.Option(
            "--output",
            "-o",
            help="Output file path (default: display in terminal)",
        ),
    ] = None,
    format: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Output format: markdown, jira, or json",
        ),
    ] = "markdown",
    post_to_jira: Annotated[
        bool,
        typer.Option(
            "--post-to-jira",
            help="Post test plan as a Jira comment",
        ),
    ] = False,
    copy: Annotated[
        bool,
        typer.Option(
            "--copy",
            "-c",
            help="Copy output to clipboard (requires pbcopy/xclip)",
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            "-q",
            help="Minimal output (useful for scripting)",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Show detailed output and API calls",
        ),
    ] = False,
):
    """
    Generate comprehensive test plans from Jira tickets.

    Automatically uses:
        - Ticket details and description
        - Development activity (commits, PRs)
        - Code changes from pull requests
        - PR discussions and reviews
        - Repository documentation
        - Figma design context (if available)

    Examples:
        testplan generate PROJ-123
        testplan generate PROJ-123 -o plan.md
        testplan generate PROJ-123 --post-to-jira
        testplan generate PROJ-123 PROJ-124 PROJ-125
    """
    # Validate format
    valid_formats = ["markdown", "jira", "json"]
    if format not in valid_formats:
        console.print(
            f"[red]✗ Invalid format:[/red] {format}. Choose from: {', '.join(valid_formats)}"
        )
        raise typer.Exit(1)

    # Check if configuration exists
    if not config_manager.is_configured():
        console.print("[red]✗ Configuration incomplete![/red]")
        console.print(
            "\nRun [cyan]testplan config set[/cyan] to configure your API tokens."
        )
        raise typer.Exit(1)

    # Load configuration and set environment variables
    config = config_manager.load()
    import os

    os.environ["JIRA_URL"] = config.jira_url or ""
    os.environ["JIRA_USERNAME"] = config.jira_email or ""
    os.environ["JIRA_API_TOKEN"] = config.jira_token or ""
    os.environ["ANTHROPIC_API_KEY"] = config.anthropic_key or ""
    os.environ["GITHUB_TOKEN"] = config.github_token or ""
    os.environ["FIGMA_TOKEN"] = config.figma_token or ""
    os.environ["LLM_PROVIDER"] = "claude"
    # LLM_MODEL is deliberately not set: Settings resolves env > .env >
    # DEFAULT_CLAUDE_MODEL, and writing it here pinned the CLI to one model
    # while the API and watcher followed .env.

    # Process each ticket
    for ticket_key in ticket_keys:
        if not quiet:
            console.print(f"\n[bold blue]Processing {ticket_key}...[/bold blue]")

        try:
            # Fetch ticket
            if verbose:
                console.print(f"[dim]Fetching ticket from Jira...[/dim]")

            jira_client = JiraClient()
            issue = asyncio.run(jira_client.get_issue(ticket_key))

            if not quiet:
                console.print(f"[green]✓[/green] Ticket fetched: {issue.summary}")

            # Generate test plan
            if not quiet:
                console.print(
                    "[bold blue]Generating test plan with Claude Opus 4.5...[/bold blue]"
                )

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                task = progress.add_task("Analyzing ticket and generating tests...", total=None)

                # One call through plan_service — the same pipeline the web
                # UI runs (deliverable classifier, four post-generation
                # critics, AC coverage, run persistence). This block used to
                # assemble its own context and call llm.generate_test_plan
                # directly, so CLI plans silently carried no critic badges
                # and no AC coverage.
                test_plan_dict = asyncio.run(
                    plan_service.generate_single(
                        GenerateTestPlanRequest(
                            **plan_service.prompt_payload(
                                plan_service.serialize_issue(issue)
                            )
                        )
                    )
                )

                progress.remove_task(task)

            if not quiet:
                console.print("[green]✓[/green] Test plan generated successfully!")

            # Format output
            formatted_output = _format_test_plan(test_plan_dict, format, issue.key)

            # Handle output
            if output:
                # Save to file
                output.write_text(formatted_output)
                if not quiet:
                    console.print(f"[green]✓[/green] Saved to: {output}")
            elif quiet:
                # Just print the raw output
                print(formatted_output)
            else:
                # Display in terminal with rich formatting
                console.print("\n")
                console.print(Panel(f"[bold]Test Plan for {issue.key}[/bold]", border_style="blue"))
                console.print()

                if format == "json":
                    syntax = Syntax(formatted_output, "json", theme="monokai")
                    console.print(syntax)
                else:
                    console.print(Markdown(formatted_output))

            # Copy to clipboard if requested
            if copy:
                _copy_to_clipboard(formatted_output)
                if not quiet:
                    console.print("[green]✓[/green] Copied to clipboard")

            # Post to Jira if requested
            if post_to_jira:
                if verbose:
                    console.print("[dim]Posting to Jira...[/dim]")

                # Use Jira format for posting
                jira_formatted = _format_test_plan(test_plan_dict, "jira", issue.key)
                result = asyncio.run(jira_client.post_comment(issue.key, jira_formatted))

                if not quiet:
                    if result.get("updated"):
                        console.print("[green]✓[/green] Test plan updated in Jira")
                    else:
                        console.print("[green]✓[/green] Test plan posted to Jira")

        except NonTestableIssueError as e:
            # Epic / Spike. A per-ticket condition, so a batch skips it and
            # keeps going rather than aborting the whole run.
            console.print(f"[yellow]⊘ Skipped:[/yellow] {e}")
            if len(ticket_keys) == 1:
                raise typer.Exit(1)
            continue
        except JiraNotFoundError:
            console.print(f"[red]✗ Ticket not found:[/red] {ticket_key}")
            if len(ticket_keys) == 1:
                raise typer.Exit(1)
            continue
        except JiraAuthError as e:
            console.print(f"[red]✗ Jira authentication failed:[/red] {e}")
            raise typer.Exit(1)
        except LLMError as e:
            console.print(f"[red]✗ LLM generation failed:[/red] {e}")
            raise typer.Exit(1)
        except Exception as e:
            console.print(f"[red]✗ Unexpected error:[/red] {e}")
            if verbose:
                import traceback

                console.print("[dim]" + traceback.format_exc() + "[/dim]")
            raise typer.Exit(1)

    if not quiet and len(ticket_keys) > 1:
        console.print(f"\n[green]✓ Processed {len(ticket_keys)} tickets successfully![/green]")


_SURFACE_LABELS = {
    "backend_http": "Backend / HTTP",
    "web_ui": "Web UI",
    "mobile": "Mobile",
    "cli": "CLI",
    "manual_only": "Manual only",
}


def _case_grounding_lines(test: dict, format: str) -> list[str]:
    """Surface/credentials/environment + the verified-vs-assumption marker.

    Emitted for every case so a runner knows what it takes to execute it and a
    reviewer knows which assertions were read from the implementation.
    """
    lines: list[str] = []

    runs_on = []
    if test.get("surface"):
        runs_on.append(_SURFACE_LABELS.get(test["surface"], test["surface"]))
    if test.get("credentials"):
        runs_on.append(f"credentials: {test['credentials']}")
    if test.get("environment"):
        runs_on.append(f"environment: {test['environment']}")
    if runs_on:
        lines.append("**Runs on:**" if format == "markdown" else "Runs on:")
        lines.append(" · ".join(runs_on))
        lines.append("")

    verified = test.get("expected_verified")
    if verified is True:
        source = test.get("expected_source") or "source not cited"
        lines.append(
            f"**Expected verified against:** `{source}`"
            if format == "markdown"
            else f"Expected verified against: {source}"
        )
        lines.append("")
    elif verified is False:
        lines.append(
            "> ⚠️ **Unverified — assumption.** This expected result was not read from the implementation; confirm it against the code before treating a failure as a defect."
            if format == "markdown"
            else "⚠ Unverified — assumption. Not read from the implementation; confirm against the code before treating a failure as a defect."
        )
        lines.append("")

    return lines


def _format_test_plan(test_plan: dict, format: str, ticket_key: str) -> str:
    """Format test plan based on output format."""
    if format == "json":
        return json.dumps(test_plan, indent=2)

    # Markdown or Jira format
    lines = []

    if format == "markdown":
        lines.append(f"# Test Plan: {ticket_key}\n")
    else:
        # For Jira format, don't add marker here - jira_client.post_comment() adds it
        lines.append("=" * 60)
        lines.append("")

    # Happy Path
    if test_plan.get("happy_path"):
        if format == "markdown":
            lines.append("## Happy Path Test Cases\n")
        else:
            lines.append("HAPPY PATH TEST CASES")
            lines.append("-" * 60)
            lines.append("")

        for i, test in enumerate(test_plan["happy_path"], 1):
            lines.append(f"### Test {i}: {test['title']}\n" if format == "markdown" else f"Test {i}: {test['title']}")
            lines.append("")
            if test.get("needs_manual_verification"):
                lines.append(
                    "> ⚠️ **Needs manual verification** — the AC element referenced here could not be verified in the PR diff or testID reference."
                    if format == "markdown"
                    else "⚠ Needs manual verification — AC element not found in PR diff/testID reference."
                )
                lines.append("")
            if test.get("preconditions"):
                lines.append("**Preconditions:**" if format == "markdown" else "Preconditions:")
                lines.append(test["preconditions"])
                lines.append("")
            lines.append("**Steps:**" if format == "markdown" else "Steps:")
            for step_num, step in enumerate(test.get("steps", []), 1):
                lines.append(f"{step_num}. {step}")
            lines.append("")
            lines.append("**Expected Result:**" if format == "markdown" else "Expected Result:")
            lines.append(test.get("expected", ""))
            lines.append("")
            if test.get("test_data"):
                lines.append("**Test Data:**" if format == "markdown" else "Test Data:")
                lines.append(test["test_data"])
                lines.append("")
            lines.extend(_case_grounding_lines(test, format))

    # Edge Cases
    if test_plan.get("edge_cases"):
        if format == "markdown":
            lines.append("## Edge Cases\n")
        else:
            lines.append("")
            lines.append("EDGE CASES")
            lines.append("-" * 60)
            lines.append("")

        for i, test in enumerate(test_plan["edge_cases"], 1):
            lines.append(f"### Test {i}: {test['title']}\n" if format == "markdown" else f"Test {i}: {test['title']}")
            lines.append("")
            if test.get("needs_manual_verification"):
                lines.append(
                    "> ⚠️ **Needs manual verification** — the AC element referenced here could not be verified in the PR diff or testID reference."
                    if format == "markdown"
                    else "⚠ Needs manual verification — AC element not found in PR diff/testID reference."
                )
                lines.append("")
            if test.get("preconditions"):
                lines.append("**Preconditions:**" if format == "markdown" else "Preconditions:")
                lines.append(test["preconditions"])
                lines.append("")
            lines.append("**Steps:**" if format == "markdown" else "Steps:")
            for step_num, step in enumerate(test.get("steps", []), 1):
                lines.append(f"{step_num}. {step}")
            lines.append("")
            lines.append("**Expected Result:**" if format == "markdown" else "Expected Result:")
            lines.append(test.get("expected", ""))
            lines.append("")
            if test.get("test_data"):
                lines.append("**Test Data:**" if format == "markdown" else "Test Data:")
                lines.append(test["test_data"])
                lines.append("")
            lines.extend(_case_grounding_lines(test, format))

    # Regression Checklist
    if test_plan.get("regression_checklist"):
        if format == "markdown":
            lines.append("## Regression Checklist\n")
        else:
            lines.append("")
            lines.append("REGRESSION CHECKLIST")
            lines.append("-" * 60)
            lines.append("")

        for item in test_plan["regression_checklist"]:
            lines.append(f"- {item}")

        lines.append("")

    # Gaps that would break the ticket's goal but that no case covers. Kept
    # out of the checklist deliberately — they aren't pass/fail items.
    if test_plan.get("risks_and_gaps"):
        if format == "markdown":
            lines.append("## ⚠️ Risks / Gaps Observed\n")
        else:
            lines.append("")
            lines.append("RISKS / GAPS OBSERVED")
            lines.append("-" * 60)
            lines.append("")
        lines.append(
            "_Not test cases — gaps in the ticket itself that no case can mark pass or fail._"
            if format == "markdown"
            else "Not test cases — gaps in the ticket itself that no case can mark pass or fail."
        )
        lines.append("")
        for gap in test_plan["risks_and_gaps"]:
            if isinstance(gap, str):
                lines.append(f"- {gap}")
                continue
            lines.append(f"- {gap.get('gap', 'Unnamed gap')}")
            if gap.get("impact"):
                lines.append(f"  - Impact: {gap['impact']}")
            if gap.get("evidence"):
                lines.append(f"  - Evidence: {gap['evidence']}")
        lines.append("")

    return "\n".join(lines)


def _copy_to_clipboard(text: str) -> None:
    """Copy text to clipboard using pbcopy (macOS) or xclip (Linux)."""
    import platform
    import subprocess

    try:
        if platform.system() == "Darwin":
            # macOS
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
        else:
            # Linux
            subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        console.print("[yellow]⚠ Clipboard copy failed (pbcopy/xclip not available)[/yellow]")
