import json
import os
from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .bug_lens_routes import router as bug_lens_router
from .config import NON_TESTABLE_ISSUE_TYPES
from .db.models.plan import PlanFormat
from .db.models.run import RunType
from .jira_client import (
    JiraAuthError,
    JiraClient,
    JiraConnectionError,
    JiraNotFoundError,
)
from .llm_client import LLMError, get_llm_client
from .models import GenerateTestPlanRequest, MultiTicketGenerateRequest, PostCommentRequest
from .runs_routes import router as runs_router
from .services import run_tracker
from .slack_client import resolve_slack_messages_in_text
from .token_service import token_health_service


def _derive_context_flags(request: GenerateTestPlanRequest) -> dict:
    dev_info = request.development_info or {}
    prs = dev_info.get("pull_requests") or []
    had_pr_diff = any(
        any((fc or {}).get("patch") for fc in (pr or {}).get("files_changed") or [])
        for pr in prs
    )
    linked = request.linked_info or {}
    linked_count = sum(
        len(v or []) for v in linked.values() if isinstance(v, list)
    )
    testing_ctx_str = json.dumps(request.testing_context or {}).lower()
    had_figma = "figma" in testing_ctx_str
    return {
        "had_pr_diff": had_pr_diff,
        "had_figma": had_figma,
        "had_parent": bool(request.parent_info),
        "linked_ticket_count": linked_count,
        "pr_count": len(prs),
        "comment_count": len(request.comments or []),
    }


def _flatten_cases_for_persistence(test_plan) -> list[tuple[str, str, str | None]]:
    cases: list[tuple[str, str, str | None]] = []

    def _structured_case_body(item: dict) -> str:
        parts = []
        if item.get("preconditions"):
            parts.append(f"Preconditions: {item['preconditions']}")
        steps = item.get("steps") or []
        if steps:
            parts.append("Steps:\n" + "\n".join(f"- {s}" for s in steps))
        if item.get("expected"):
            parts.append(f"Expected: {item['expected']}")
        if item.get("test_data"):
            parts.append(f"Test data: {item['test_data']}")
        return "\n\n".join(parts)

    for item in test_plan.happy_path or []:
        if isinstance(item, dict):
            cases.append((item.get("title", ""), _structured_case_body(item), "happy_path"))
    for item in test_plan.edge_cases or []:
        if isinstance(item, dict):
            category = f"edge:{item.get('category', 'edge')}"
            cases.append((item.get("title", ""), _structured_case_body(item), category))
    for item in test_plan.integration_tests or []:
        if isinstance(item, dict):
            cases.append((item.get("title", ""), _structured_case_body(item), "integration"))
    for item in test_plan.regression_checklist or []:
        if isinstance(item, str):
            cases.append((item, "", "regression"))

    return cases

app = FastAPI(title="Jira Test Plan Bot", version="0.1.0")
app.include_router(bug_lens_router)
app.include_router(runs_router)

# Configure CORS for frontend communication
# NOTE: For production, update allow_origins to include your production URLs
# or configure via environment variable (e.g., settings.cors_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite dev server default port
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/tokens")
async def check_tokens():
    """
    Check health status of all API tokens.

    Returns detailed status for:
    - Jira API Token (required)
    - GitHub Personal Access Token (optional)
    - Anthropic/Claude API Key (required when using Claude provider)

    Each token status includes:
    - service_name: Name of the service
    - is_valid: Whether the token is valid
    - is_required: Whether this service is required
    - error_type: Type of error if invalid (expired, invalid, missing, etc.)
    - error_message: Detailed error message
    - help_url: URL for generating/managing the token
    - last_checked: Timestamp of the check
    """
    token_statuses = await token_health_service.validate_all_tokens()

    # Convert to dict for JSON response
    services = []
    for status in token_statuses:
        services.append({
            "service_name": status.service_name,
            "is_valid": status.is_valid,
            "is_required": status.is_required,
            "error_type": status.error_type.value if status.error_type else None,
            "error_message": status.error_message,
            "help_url": status.help_url,
            "last_checked": status.last_checked.isoformat() if status.last_checked else None,
            "details": status.details,
        })

    return {
        "services": services,
        "overall_health": all(
            s.is_valid or not s.is_required for s in token_statuses
        ),  # Overall health is OK if all required services are valid
    }


@app.get("/config")
def get_config():
    """Get public configuration for frontend (Jira base URL for ticket links)."""
    from .config import settings

    return {
        "jira_base_url": settings.jira_url,
    }


@app.get("/issue/{issue_key}")
async def get_issue(issue_key: str):
    jira = JiraClient()
    try:
        issue = await jira.get_issue(issue_key)

        # Serialize development info if available
        development_info_dict = None
        if issue.development_info:
            development_info_dict = {
                "commits": [asdict(commit) for commit in issue.development_info.commits],
                "pull_requests": [asdict(pr) for pr in issue.development_info.pull_requests],
                "branches": issue.development_info.branches,
                "repository_context": asdict(issue.development_info.repository_context) if issue.development_info.repository_context else None,
                "figma_context": asdict(issue.development_info.figma_context) if issue.development_info.figma_context else None,
            }

        # Serialize attachments if available
        attachments_list = None
        if issue.attachments:
            attachments_list = [asdict(attachment) for attachment in issue.attachments]

        # Serialize comments if available
        comments_list = None
        if issue.comments:
            comments_list = [asdict(comment) for comment in issue.comments]

        # Serialize parent info if available
        parent_info_dict = None
        if issue.parent:
            parent_info_dict = {
                "key": issue.parent.key,
                "summary": issue.parent.summary,
                "description": issue.parent.description,
                "issue_type": issue.parent.issue_type,
                "labels": issue.parent.labels,
            }
            # Include parent attachments if available
            if issue.parent.attachments:
                parent_info_dict["attachments"] = [asdict(att) for att in issue.parent.attachments]
            # Include parent Figma context if available
            if issue.parent.figma_context:
                parent_info_dict["figma_context"] = asdict(issue.parent.figma_context)

        # Serialize linked issues if available
        linked_info_dict = None
        if issue.linked_issues:
            linked_info_dict = {}
            if issue.linked_issues.blocks:
                linked_info_dict["blocks"] = [asdict(link) for link in issue.linked_issues.blocks]
            if issue.linked_issues.blocked_by:
                linked_info_dict["blocked_by"] = [asdict(link) for link in issue.linked_issues.blocked_by]
            if issue.linked_issues.causes:
                linked_info_dict["causes"] = [asdict(link) for link in issue.linked_issues.causes]
            if issue.linked_issues.caused_by:
                linked_info_dict["caused_by"] = [asdict(link) for link in issue.linked_issues.caused_by]

        return {
            "key": issue.key,
            "summary": issue.summary,
            "description": issue.description,
            "labels": issue.labels,
            "issue_type": issue.issue_type,
            "assignee": issue.assignee,
            "assignee_history": issue.assignee_history,
            "description_quality": {
                "has_description": issue.description_analysis.has_description,
                "is_weak": issue.description_analysis.is_weak,
                "warnings": issue.description_analysis.warnings,
                "char_count": issue.description_analysis.char_count,
                "word_count": issue.description_analysis.word_count,
            },
            "development_info": development_info_dict,
            "attachments": attachments_list,
            "comments": comments_list,
            "parent": parent_info_dict,
            "linked_issues": linked_info_dict,
            "status": issue.status,
            "status_category": issue.status_category,
        }
    except JiraNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except JiraAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except JiraConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except (TypeError, ValueError, AttributeError) as e:
        # Handle serialization errors (asdict() failures, malformed dataclasses, etc.)
        import logging
        logging.error(f"Serialization error for issue {issue_key}: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to serialize issue data: {type(e).__name__}"
        )
    except Exception as e:
        # Catch-all for unexpected errors
        import logging
        logging.error(f"Unexpected error fetching issue {issue_key}: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while fetching the issue"
        )


@app.post("/issue/{issue_key}/summarize")
async def summarize_issue(issue_key: str, request: dict):
    """Generate a plain-language summary of a ticket for quick tester context."""
    summary = request.get("summary", "")
    description = request.get("description")
    if not summary:
        raise HTTPException(status_code=400, detail="summary is required")
    try:
        llm = get_llm_client()
        text = await llm.summarize_ticket(summary=summary, description=description)
        return {"summary": text}
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/generate-test-plan")
async def generate_test_plan(request: GenerateTestPlanRequest):
    """
    Generate a structured test plan using LLM.

    This endpoint accepts ticket data and optional testing context,
    then uses the configured LLM provider (Ollama or Claude) to generate
    a comprehensive test plan.
    """
    if request.issue_type in NON_TESTABLE_ISSUE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Test plans are not generated for {request.issue_type} issues. "
            f"Only Story, Task, and Bug issues are supported.",
        )

    flags = _derive_context_flags(request)
    parent_key = (request.parent_info or {}).get("key")
    parent_key_clean = parent_key if isinstance(parent_key, str) and parent_key.strip() else None
    run_ctx = await run_tracker.start_run(
        run_type=RunType.test_plan,
        ticket_keys=[request.ticket_key],
        model=os.environ.get("LLM_MODEL", "unknown"),
        llm_provider=os.environ.get("LLM_PROVIDER", "unknown"),
        ticket_title=request.summary,
        ticket_issue_type=request.issue_type,
        ticket_parent_key=parent_key_clean,
        **flags,
    )

    try:
        images = None
        if request.image_urls:
            jira = JiraClient()
            images = []
            for image_url in request.image_urls[:3]:
                image_data = await jira.download_image_as_base64(image_url)
                if image_data:
                    images.append(image_data)
            if not images:
                images = None

        resolved_slack = await resolve_slack_messages_in_text(
            request.description, request.comments
        )
        slack_messages_for_prompt = (
            [asdict(m) for m in resolved_slack] if resolved_slack else None
        )

        llm = get_llm_client()
        test_plan = await llm.generate_test_plan(
            ticket_key=request.ticket_key,
            summary=request.summary,
            description=request.description,
            testing_context=request.testing_context,
            development_info=request.development_info,
            images=images,
            comments=request.comments,
            parent_info=request.parent_info,
            linked_info=request.linked_info,
            slack_messages=slack_messages_for_prompt,
        )

        response = {
            "ticket_key": request.ticket_key,
            "happy_path": test_plan.happy_path,
            "edge_cases": test_plan.edge_cases,
            "regression_checklist": test_plan.regression_checklist,
            "integration_tests": test_plan.integration_tests or [],
        }

        await run_tracker.complete_with_plan(
            run_ctx,
            plan_body=json.dumps(response),
            plan_format=PlanFormat.json,
            cases=_flatten_cases_for_persistence(test_plan),
        )
        return response

    except LLMError as e:
        await run_tracker.fail(run_ctx, error_code=f"LLMError: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        await run_tracker.fail(run_ctx, error_code=f"{type(e).__name__}: {e}")
        raise


def _check_tickets_share_context(tickets: list) -> bool:
    """Return True if at least two tickets share a repository or an overlapping file."""
    ticket_repos: list[set] = []
    ticket_files: list[set] = []

    for ticket in tickets:
        dev_info = ticket.development_info if hasattr(ticket, "development_info") else None
        if not dev_info:
            continue
        repos: set[str] = set()
        files: set[str] = set()
        for pr in dev_info.get("pull_requests", []):
            if pr.get("repository"):
                repos.add(pr["repository"])
            for fc in pr.get("files_changed", []):
                if fc.get("filename"):
                    files.add(fc["filename"])
        if repos or files:
            ticket_repos.append(repos)
            ticket_files.append(files)

    if len(ticket_repos) < 2:
        return False

    for i in range(len(ticket_repos)):
        for j in range(i + 1, len(ticket_repos)):
            if ticket_repos[i] & ticket_repos[j]:
                return True
            if ticket_files[i] & ticket_files[j]:
                return True

    return False


@app.post("/generate-test-plan/multi")
async def generate_multi_ticket_test_plan(request: MultiTicketGenerateRequest):
    """
    Generate a unified test plan for multiple related Jira tickets.

    Tickets must share code changes (same repository or overlapping files).
    Returns 422 with TICKETS_NO_SHARED_CONTEXT when no overlap is detected.
    """
    for ticket in request.tickets:
        if ticket.issue_type in NON_TESTABLE_ISSUE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Test plans are not generated for {ticket.issue_type} issues ({ticket.ticket_key}).",
            )

    if not _check_tickets_share_context(request.tickets):
        raise HTTPException(
            status_code=422,
            detail="TICKETS_NO_SHARED_CONTEXT",
        )

    try:
        # Collect images from all tickets (cap at 3 total)
        all_images: list | None = None
        jira = JiraClient()
        for ticket in request.tickets:
            if ticket.image_urls and (all_images is None or len(all_images) < 3):
                for url in ticket.image_urls:
                    if all_images is not None and len(all_images) >= 3:
                        break
                    image_data = await jira.download_image_as_base64(url)
                    if image_data:
                        if all_images is None:
                            all_images = []
                        all_images.append(image_data)

        tickets_data = [
            {
                "ticket_key": t.ticket_key,
                "summary": t.summary,
                "description": t.description,
                "issue_type": t.issue_type,
                "testing_context": t.testing_context,
                "development_info": t.development_info,
                "comments": t.comments,
                "parent_info": t.parent_info,
                "linked_info": t.linked_info,
            }
            for t in request.tickets
        ]

        llm = get_llm_client()
        test_plan = await llm.generate_multi_ticket_test_plan(
            tickets=tickets_data,
            images=all_images,
        )

        return {
            "ticket_keys": [t.ticket_key for t in request.tickets],
            "happy_path": test_plan.happy_path,
            "edge_cases": test_plan.edge_cases,
            "regression_checklist": test_plan.regression_checklist,
            "integration_tests": test_plan.integration_tests or [],
        }
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/jira/post-comment")
async def post_comment(request: PostCommentRequest):
    """
    Post a comment to a Jira issue.

    This endpoint posts the provided text as a comment on the specified Jira issue.
    Typically used to post generated test plans back to the ticket.
    """
    jira = JiraClient()
    try:
        result = await jira.post_comment(request.issue_key, request.comment_text)
        return {
            "success": True,
            "comment_id": result.get("id"),
            "issue_key": request.issue_key,
            "updated": result.get("updated", False),
        }
    except JiraNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except JiraAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except JiraConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        # Catch-all for unexpected errors
        import logging
        logging.error(f"Unexpected error posting comment to {request.issue_key}: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while posting the comment"
        )
