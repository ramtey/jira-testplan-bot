"""MCP Server for Jira Test Plan Bot.

This MCP server exposes test plan generation functionality
to Claude desktop app and other MCP-compatible clients.
"""

import asyncio
import json
import os
from dataclasses import asdict
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from ..app.jira_client import JiraClient, JiraAuthError, JiraNotFoundError
from ..app.llm_client import get_llm_client, LLMError
from ..app.token_service import TokenHealthService

# Initialize MCP server
app = Server("jira-testplan-bot")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="fetch_jira_ticket",
            description=(
                "Fetch a Jira ticket with its details, description, and development activity "
                "(commits, pull requests, branches). Returns ticket summary, description, labels, "
                "issue type, and all linked development information."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_key": {
                        "type": "string",
                        "description": "Jira ticket key (e.g., PROJ-123, ABC-456)",
                    }
                },
                "required": ["ticket_key"],
            },
        ),
        Tool(
            name="generate_test_plan",
            description=(
                "Generate a comprehensive test plan for a Jira ticket. "
                "Automatically fetches ticket details, development activity (commits, PRs, code changes), "
                "and uses AI to create structured test cases with happy path, edge cases, "
                "and regression checklist. Returns test plan in markdown format. "
                "IMPORTANT: Display the complete test plan exactly as returned - do not summarize or condense it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_key": {
                        "type": "string",
                        "description": "Jira ticket key (e.g., PROJ-123, ABC-456)",
                    }
                },
                "required": ["ticket_key"],
            },
        ),
        Tool(
            name="check_token_health",
            description=(
                "Check the health status of all configured API tokens "
                "(Jira, Claude/Anthropic, GitHub, Figma). "
                "Returns validation status, error messages, and help URLs for each service."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Handle tool calls."""

    # Validate environment configuration
    _validate_environment()

    if name == "fetch_jira_ticket":
        return await _fetch_jira_ticket(arguments["ticket_key"])
    elif name == "generate_test_plan":
        return await _generate_test_plan(arguments["ticket_key"])
    elif name == "check_token_health":
        return await _check_token_health()
    else:
        raise ValueError(f"Unknown tool: {name}")


async def _fetch_jira_ticket(ticket_key: str) -> list[TextContent]:
    """Fetch Jira ticket details."""
    try:
        jira_client = JiraClient()
        issue = await jira_client.get_issue(ticket_key)

        # Format the response
        output = [
            f"# {issue.key}: {issue.summary}",
            "",
            f"**Type:** {issue.issue_type}",
            f"**Labels:** {', '.join(issue.labels) if issue.labels else 'None'}",
            "",
            "## Description",
            issue.description or "*No description provided*",
            "",
        ]

        # Add development info if available
        if issue.development_info:
            dev_info = issue.development_info

            if dev_info.pull_requests:
                output.append("## Pull Requests")
                for pr in dev_info.pull_requests:
                    output.append(f"- **{pr.title}** ({pr.status})")
                    output.append(f"  - Branch: {pr.source_branch} → {pr.destination_branch}")
                    output.append(f"  - URL: {pr.url}")
                output.append("")

            if dev_info.commits:
                output.append(f"## Commits ({len(dev_info.commits)} total)")
                for commit in dev_info.commits[:5]:  # Show first 5
                    output.append(f"- {commit.message}")
                if len(dev_info.commits) > 5:
                    output.append(f"  *...and {len(dev_info.commits) - 5} more*")
                output.append("")

            if dev_info.branches:
                output.append("## Branches")
                for branch in dev_info.branches:
                    output.append(f"- {branch}")
                output.append("")

        return [TextContent(type="text", text="\n".join(output))]

    except JiraNotFoundError:
        return [TextContent(
            type="text",
            text=f"❌ Ticket not found: {ticket_key}\n\nPlease check the ticket key and try again."
        )]
    except JiraAuthError as e:
        return [TextContent(
            type="text",
            text=f"❌ Jira authentication failed: {e}\n\nCheck your JIRA_API_TOKEN environment variable."
        )]
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"❌ Error fetching ticket: {e}"
        )]


async def _generate_test_plan(ticket_key: str) -> list[TextContent]:
    """Generate test plan for a Jira ticket."""
    try:
        # Fetch ticket
        jira_client = JiraClient()
        issue = await jira_client.get_issue(ticket_key)

        # Prepare development info
        development_info = None
        if issue.development_info:
            development_info = asdict(issue.development_info)

        # Generate test plan
        llm_client = get_llm_client()
        test_plan = await llm_client.generate_test_plan(
            ticket_key=issue.key,
            summary=issue.summary,
            description=issue.description or "",
            testing_context={},
            development_info=development_info,
        )

        # Convert to dict for formatting
        test_plan_dict = asdict(test_plan)

        # Format as markdown
        output = [
            "📋 **COMPLETE TEST PLAN** - Display this entire document without summarizing",
            "",
            f"# Test Plan: {issue.key}",
            "",
            f"**Ticket:** {issue.summary}",
            "",
        ]

        # Happy Path
        if test_plan_dict.get("happy_path"):
            output.append("## Happy Path Test Cases")
            output.append("")
            for i, test in enumerate(test_plan_dict["happy_path"], 1):
                output.append(f"### Test {i}: {test['title']}")
                output.append("")
                output.append("**Steps:**")
                for step_num, step in enumerate(test.get("steps", []), 1):
                    output.append(f"{step_num}. {step}")
                output.append("")
                output.append("**Expected Result:**")
                output.append(test.get("expected", ""))
                output.append("")

        # Edge Cases
        if test_plan_dict.get("edge_cases"):
            output.append("## Edge Cases")
            output.append("")
            for i, test in enumerate(test_plan_dict["edge_cases"], 1):
                output.append(f"### Test {i}: {test['title']}")
                output.append("")
                output.append("**Steps:**")
                for step_num, step in enumerate(test.get("steps", []), 1):
                    output.append(f"{step_num}. {step}")
                output.append("")
                output.append("**Expected Result:**")
                output.append(test.get("expected", ""))
                output.append("")

        # Regression Checklist
        if test_plan_dict.get("regression_checklist"):
            output.append("## Regression Checklist")
            output.append("")
            for item in test_plan_dict["regression_checklist"]:
                output.append(f"- {item}")
            output.append("")

        output.append("---")
        output.append("")
        output.append("*Generated with Claude Opus 4.5*")

        return [TextContent(type="text", text="\n".join(output))]

    except JiraNotFoundError:
        return [TextContent(
            type="text",
            text=f"❌ Ticket not found: {ticket_key}\n\nPlease check the ticket key and try again."
        )]
    except JiraAuthError as e:
        return [TextContent(
            type="text",
            text=f"❌ Jira authentication failed: {e}\n\nCheck your JIRA_API_TOKEN environment variable."
        )]
    except LLMError as e:
        return [TextContent(
            type="text",
            text=f"❌ Test plan generation failed: {e}\n\nCheck your ANTHROPIC_API_KEY environment variable."
        )]
    except Exception as e:
        return [TextContent(
            type="text",
            text=f"❌ Error generating test plan: {e}"
        )]


async def _check_token_health() -> list[TextContent]:
    """Check health status of all API tokens."""
    try:
        token_service = TokenHealthService()
        token_statuses = await token_service.validate_all_tokens()

        output = ["# API Token Health Status", ""]

        for status in token_statuses:
            if status.is_valid:
                output.append(f"✅ **{status.service_name}**: Valid")
                if status.details:
                    for key, value in status.details.items():
                        output.append(f"   - {key}: {value}")
            else:
                icon = "❌" if status.is_required else "⚠️"
                output.append(f"{icon} **{status.service_name}**: {status.error_type}")
                output.append(f"   - Error: {status.error_message}")
                output.append(f"   - Help: {status.help_url}")
            output.append("")

        # Overall status
        all_required_valid = all(
            status.is_valid for status in token_statuses if status.is_required
        )

        if all_required_valid:
            output.append("✅ **Overall Status:** All required services are configured correctly")
        else:
            output.append("❌ **Overall Status:** Some required services have issues")

        return [TextContent(type="text", text="\n".join(output))]

    except Exception as e:
        return [TextContent(
            type="text",
            text=f"❌ Error checking token health: {e}"
        )]


def _validate_environment():
    """Validate that required environment variables are set."""
    required_vars = {
        "JIRA_BASE_URL": "Jira base URL (e.g., https://company.atlassian.net)",
        "JIRA_EMAIL": "Jira account email",
        "JIRA_API_TOKEN": "Jira API token",
        "ANTHROPIC_API_KEY": "Anthropic/Claude API key",
    }

    # Set default LLM provider and model if not set
    if not os.getenv("LLM_PROVIDER"):
        os.environ["LLM_PROVIDER"] = "claude"
    if not os.getenv("LLM_MODEL"):
        os.environ["LLM_MODEL"] = "claude-opus-4-5-20251101"

    missing = []
    for var, description in required_vars.items():
        if not os.getenv(var):
            missing.append(f"  - {var}: {description}")

    if missing:
        error_msg = (
            "Missing required environment variables:\n" + "\n".join(missing) +
            "\n\nPlease set these variables before running the MCP server."
        )
        raise ValueError(error_msg)


async def _async_main():
    """Main async entry point."""
    # Run the server
    # Note: Environment variables will be passed by Claude desktop
    # and validated when tools are called
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )


def main():
    """Main entry point for the MCP server."""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
