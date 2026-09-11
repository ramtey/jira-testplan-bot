import asyncio
import json
from dataclasses import asdict

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .bug_lens_routes import router as bug_lens_router
from .config import settings
from .db.models.ticket_hold import HOLD_REASONS
from .db.session import get_sessionmaker
from .jira_client import (
    JiraAuthError,
    JiraClient,
    JiraConnectionError,
    JiraContentLimitError,
    JiraNotFoundError,
)
from .llm_client import LLMError, get_llm_client
from .models import (
    GenerateTestPlanRequest,
    MultiTicketGenerateRequest,
    PostCommentRequest,
    TestPlanProgressUpdateRequest,
    TicketHoldRequest,
    WalkthroughUpdateRequest,
)
from .repositories import (
    plan_repository,
    test_plan_progress_repository,
    ticket_hold_repository,
    walkthrough_repository,
)
from .runs_routes import router as runs_router
from .services import plan_service
from .services.plan_service import NonTestableIssueError, SourceLookupUnavailableError
from .services.test_plan_generator import (
    classify_deliverable,
    compute_ac_coverage,
    derive_context_flags,
    flatten_cases_for_persistence,
    normalize_grounding_warnings,
    run_code_grounding_critic,
    run_fix_scope_critic,
    run_grounding_critic,
    run_surface_mismatch_critic,
)
from .token_service import token_health_service
from . import uat_readiness
from .workflow_routes import router as workflow_router

# Backward-compat aliases — tests import these underscored names from
# src.app.main. The canonical home is services.test_plan_generator.
_derive_context_flags = derive_context_flags
_normalize_grounding_warnings = normalize_grounding_warnings
_compute_ac_coverage = compute_ac_coverage
_run_grounding_critic = run_grounding_critic
_run_code_grounding_critic = run_code_grounding_critic
_run_fix_scope_critic = run_fix_scope_critic
_classify_deliverable = classify_deliverable
_run_surface_mismatch_critic = run_surface_mismatch_critic
_flatten_cases_for_persistence = flatten_cases_for_persistence


app = FastAPI(title="Jira Test Plan Bot", version="0.1.0")
app.include_router(bug_lens_router)
app.include_router(runs_router)
app.include_router(workflow_router)

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
        "workflow_project_prefixes": settings.workflow_project_prefixes,
    }


async def _safe_account_id_for(jira: JiraClient):
    """Best-effort /myself lookup used by both /issue/{key} and /issue/{key}/basic."""
    try:
        return await jira.get_my_account_id()
    except Exception:
        return None


@app.get("/issue/{issue_key}/basic")
async def get_issue_basic(issue_key: str):
    """
    Fast progressive-load endpoint: returns the ticket's base data (description,
    labels, status, assignee, story points, attachments, description-quality
    gaps) without waiting on network-heavy enrichment (dev status API, comments,
    Figma, parent, children, linked issues, bounce history). The response shape
    matches /issue/{key} — enrichment-derived fields are explicitly null so the
    frontend can render "loading" placeholders for those sections and swap them
    in when the full /issue/{key} response lands.
    """
    jira = JiraClient()
    try:
        issue, current_user_account_id = await asyncio.gather(
            jira.get_issue_basic(issue_key),
            _safe_account_id_for(jira),
        )
        attachments_list = (
            [asdict(a) for a in issue.attachments] if issue.attachments else None
        )
        return {
            "key": issue.key,
            "summary": issue.summary,
            "description": issue.description,
            "labels": issue.labels,
            "issue_type": issue.issue_type,
            "assignee": issue.assignee,
            "assignee_account_id": issue.assignee_account_id,
            "assignee_history": issue.assignee_history,
            "assignee_history_account_ids": issue.assignee_history_account_ids,
            "current_user_account_id": current_user_account_id,
            "description_quality": {
                "has_description": issue.description_analysis.has_description,
                "gaps": issue.description_analysis.gaps,
                "char_count": issue.description_analysis.char_count,
                "word_count": issue.description_analysis.word_count,
            },
            # Enrichment fields — the full /issue/{key} response will fill these in.
            "development_info": None,
            "attachments": attachments_list,
            "comments": None,
            "parent": None,
            "children": None,
            "linked_issues": None,
            "status": issue.status,
            "status_category": issue.status_category,
            "bounce_history": None,
            "story_points": issue.story_points,
            # Marker so the frontend can distinguish a partial ticket from a
            # full one (e.g. show a subtle "loading enrichment" indicator).
            "is_partial": True,
        }
    except JiraNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except JiraAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except JiraConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/issue/{issue_key}")
async def get_issue(issue_key: str):
    jira = JiraClient()
    try:
        issue, current_user_account_id = await asyncio.gather(
            jira.get_issue(issue_key),
            _safe_account_id_for(jira),
        )

        # Serialization lives in plan_service so a server-side generate can
        # rebuild this exact payload without a browser round-trip.
        return {
            **plan_service.serialize_issue(issue),
            "current_user_account_id": current_user_account_id,
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


@app.get("/issue/{epic_key}/children")
async def get_epic_children(epic_key: str):
    """List child tickets under an Epic.

    Returns lightweight rows (key, summary, issue_type, status) so the UI can
    render an inline list with per-row Generate/Analyze actions.
    """
    jira = JiraClient()
    try:
        children = await jira.search_epic_children(epic_key)
        return {"children": [asdict(child) for child in children]}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except JiraAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except JiraConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        import logging
        logging.error(f"Unexpected error fetching children for {epic_key}: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while fetching Epic children",
        )


@app.get("/jira/projects")
async def list_jira_projects(active_within_days: int | None = None):
    """List Jira projects accessible to the configured account.

    Optional `active_within_days` runs a JQL sweep for issues updated within
    that window and returns the distinct project keys alongside the full
    list, so the sidebar can filter to recently active projects while
    keeping the full set one toggle away.
    """
    jira = JiraClient()
    try:
        projects_task = jira.list_projects()
        if active_within_days and active_within_days > 0:
            active_task = jira.list_active_project_keys(days=active_within_days)
            projects, active_keys = await asyncio.gather(projects_task, active_task)
        else:
            projects = await projects_task
            active_keys = None
        return {"projects": projects, "active_keys": active_keys}
    except JiraAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except JiraConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        import logging
        logging.error(f"Unexpected error listing projects: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while listing projects",
        )


@app.get("/jira/projects/{project_key}/statuses")
async def list_jira_project_statuses(project_key: str):
    """List the unique status columns available for a project."""
    jira = JiraClient()
    try:
        statuses = await jira.list_project_statuses(project_key)
        return {"statuses": statuses}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except JiraNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except JiraAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except JiraConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        import logging
        logging.error(f"Unexpected error listing statuses for {project_key}: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while listing project statuses",
        )


@app.get("/jira/projects/{project_key}/status-counts")
async def list_jira_project_status_counts(
    project_key: str, statuses: list[str] = Query(default_factory=list)
):
    """Return approximate issue counts per status name for a project.

    Approximate counts are cheap (one Lucene-index call per status, in
    parallel) and only power sidebar badges — the exact count isn't
    load-bearing anywhere.
    """
    jira = JiraClient()
    try:
        counts = await jira.count_project_issues_by_status(project_key, statuses)
        return {"counts": counts}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except JiraAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except JiraConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        import logging
        logging.error(
            f"Unexpected error counting statuses for {project_key}: "
            f"{type(e).__name__}: {e}"
        )
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while counting project statuses",
        )


@app.get("/jira/projects/{project_key}/issues")
async def list_jira_project_issues(project_key: str, status: str):
    """List issues in a project filtered by status name."""
    if not status or not status.strip():
        raise HTTPException(status_code=400, detail="status query param is required")
    jira = JiraClient()
    try:
        issues = await jira.search_project_issues(project_key, status.strip())
        return {"issues": [asdict(issue) for issue in issues]}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except JiraAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except JiraConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        import logging
        logging.error(
            f"Unexpected error searching issues for {project_key} status={status}: {type(e).__name__}: {e}"
        )
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while searching issues",
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


@app.post("/bounce/summarize")
async def summarize_bounce(request: dict):
    """Extract a one-sentence headline explaining why a ticket bounced back.

    Expects `{"from_status", "to_status", "reason"}` and returns
    `{"headline": str | null}`. Returns `null` when the picked comment did not
    actually explain the bounce (the LLM returned NO_REASON); the UI falls
    back to showing the raw comment in that case.
    """
    from_status = (request.get("from_status") or "").strip()
    to_status = (request.get("to_status") or "").strip()
    reason = (request.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")
    if not from_status or not to_status:
        raise HTTPException(status_code=400, detail="from_status and to_status are required")
    try:
        llm = get_llm_client()
        text = await llm.summarize_bounce_reason(
            from_status=from_status, to_status=to_status, reason_text=reason
        )
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e))
    cleaned = text.strip().strip('"').strip("'")
    if not cleaned or cleaned.upper().startswith("NO_REASON"):
        return {"headline": None}
    return {"headline": cleaned}


@app.post("/bounce/summarize-changes")
async def summarize_bounce_changes(request: dict):
    """Summarize what a follow-up PR actually changed after a silent send-back.

    Called when a bounce transition had no reviewer comment attached, so the
    UI would otherwise show a bare "No comment was posted" placeholder. The
    file list + captured diffs are our best signal for what got fixed.

    Expects `{"pr_title": str | None, "files_changed": [{...}, ...]}` and
    returns `{"summary": str | null}`. Returns `null` when the model decided
    the diffs weren't clear enough to summarize honestly (NO_SUMMARY).
    """
    files_changed = request.get("files_changed") or []
    if not isinstance(files_changed, list) or not files_changed:
        raise HTTPException(status_code=400, detail="files_changed is required")
    pr_title = request.get("pr_title")
    if pr_title is not None and not isinstance(pr_title, str):
        raise HTTPException(status_code=400, detail="pr_title must be a string when provided")
    try:
        llm = get_llm_client()
        text = await llm.summarize_pr_changes(pr_title=pr_title, files_changed=files_changed)
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e))
    cleaned = text.strip().strip('"').strip("'")
    if not cleaned or cleaned.upper().startswith("NO_SUMMARY"):
        return {"summary": None}
    return {"summary": cleaned}


@app.post("/issues/summarize-batch")
async def summarize_issues_batch(request: dict):
    """Summarize a bundle of related tickets in one LLM call.

    Expects `{"tickets": [{"key", "summary", "description"?}, ...]}` and
    returns `{"overview": str, "per_ticket": [{"key", "blurb"}, ...]}`.
    Used by the multi-ticket view to give the tester a single shared
    narrative across the batch plus a one-liner per row.
    """
    raw_tickets = request.get("tickets")
    if not isinstance(raw_tickets, list) or not raw_tickets:
        raise HTTPException(
            status_code=400, detail="tickets is required and must be a non-empty list"
        )
    tickets: list[dict] = []
    for item in raw_tickets:
        if not isinstance(item, dict):
            continue
        key = (item.get("key") or "").strip()
        summary = (item.get("summary") or "").strip()
        if not key or not summary:
            continue
        tickets.append(
            {"key": key, "summary": summary, "description": item.get("description")}
        )
    if not tickets:
        raise HTTPException(
            status_code=400, detail="No tickets had both a key and a summary."
        )
    try:
        llm = get_llm_client()
        return await llm.summarize_batch(tickets)
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/generate-test-plan")
async def generate_test_plan(request: GenerateTestPlanRequest):
    """Generate a structured test plan from an already-assembled ticket payload.

    The browser holds the ticket data it fetched from ``GET /issue/{key}``, so
    it posts that context back here. Callers that only have a key should use
    ``POST /tickets/{ticket_key}/plan`` instead of re-implementing the
    assembly — see ``services/plan_service`` for why that matters.
    """
    try:
        return await plan_service.generate_single(request)
    except NonTestableIssueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SourceLookupUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/generate-test-plan/multi")
async def generate_multi_ticket_test_plan(request: MultiTicketGenerateRequest):
    """Generate a unified test plan for multiple related Jira tickets.

    Two modes:
    - **single_repo**: all tickets touch the same repository (or have no
      development info). One unified plan keyed off shared files.
    - **cross_project**: tickets span multiple repositories. A seam catalog
      of verified producer/consumer pairs is fed into the prompt so the LLM
      writes integration tests against the seams, not just per-side
      behaviour.
    """
    try:
        return await plan_service.generate_multi(request.tickets)
    except NonTestableIssueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SourceLookupUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/tickets/{ticket_key}/plan")
async def generate_plan_for_ticket(ticket_key: str):
    """Generate a test plan from a ticket *key* alone — the automation door.

    Fetches the ticket's context server-side (dev info, comments, parent,
    children, linked issues, bounce history) and runs the identical pipeline
    the browser flow uses. Comma-separate keys for a unified multi-ticket
    plan.
    """
    keys = [k.strip().upper() for k in ticket_key.split(",") if k.strip()]
    if not keys:
        raise HTTPException(status_code=400, detail="No ticket key supplied.")
    try:
        return await plan_service.generate_for_tickets(keys)
    except NonTestableIssueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SourceLookupUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except JiraNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except JiraAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except JiraConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
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
        comment_id = result.get("id")
        posted_at_iso: str | None = None
        if request.plan_id is not None and comment_id:
            try:
                sessionmaker = get_sessionmaker()
                async with sessionmaker() as session:
                    await plan_repository.mark_plan_posted_to_jira(
                        session,
                        plan_id=request.plan_id,
                        ticket_key=request.issue_key.upper(),
                        jira_comment_id=str(comment_id),
                    )
                    plan_with_cases = await plan_repository.get_plan_with_cases(
                        session, plan_id=request.plan_id
                    )
                    if plan_with_cases and plan_with_cases[0].posted_at:
                        posted_at_iso = plan_with_cases[0].posted_at.isoformat()
            except Exception:
                # Posting succeeded; failing to record the mark shouldn't fail
                # the request. The next post attempt will re-record.
                import logging
                logging.exception(
                    "Failed to mark plan %s posted on %s",
                    request.plan_id,
                    request.issue_key,
                )
        return {
            "success": True,
            "comment_id": comment_id,
            "issue_key": request.issue_key,
            "updated": result.get("updated", False),
            "plan_id": request.plan_id,
            "posted_at": posted_at_iso,
        }
    except JiraNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except JiraAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except JiraConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except JiraContentLimitError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except Exception as e:
        # Catch-all for unexpected errors
        import logging
        logging.exception(f"Unexpected error posting comment to {request.issue_key}: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while posting the comment"
        )


# Walkthrough serialization + readiness composition lives in
# ``src.app.uat_readiness`` so the workflow route (which enforces the
# ``needs_walkthrough`` rule server-side) computes it identically.


_WALKTHROUGH_ALLOWED_IMAGE_MIME = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
    "application/pdf",
}
_WALKTHROUGH_MAX_IMAGE_BYTES = 10 * 1024 * 1024


@app.get("/tickets/{ticket_key}/walkthrough")
async def get_ticket_walkthrough(ticket_key: str):
    """Return the human-authored walkthrough (Loom link, screenshot, notes) for a
    ticket. Kept apart from the generated plan so it survives regeneration.

    Also echoes the ticket's latest known ``uat_complexity`` (from the most
    recent generated plan) so the workflow UI can decide whether to nudge for a
    walkthrough when the ticket is passed to UAT.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return await uat_readiness.fetch_readiness(session, ticket_key=ticket_key)


@app.put("/tickets/{ticket_key}/walkthrough")
async def put_ticket_walkthrough(
    ticket_key: str,
    payload: str | None = Form(default=None),
    screenshots: list[UploadFile] | None = File(default=None),
):
    """Create or update a ticket's walkthrough.

    Sent as multipart/form-data: ``payload`` holds a JSON
    :class:`WalkthroughUpdateRequest` with the text fields plus
    ``existing_screenshots`` — the subset of previously-uploaded screenshots
    the client wants to keep. ``screenshots[]`` carries any new files, which
    are uploaded to Jira as attachments on the ticket. The final stored list
    is ``existing_screenshots ++ newly_uploaded`` — anything the client
    omitted from ``existing_screenshots`` drops out of the walkthrough (the
    Jira attachment itself stays on the ticket).

    Each screenshot in the final list is enumerated as a ``📷 <filename>``
    plain-text callout in the pass-to-UAT comment (same treatment as files
    attached from the UAT modal); Jira's Attachments panel renders the
    actual images right under the comment, so the "how to test this"
    guidance ships with the transition.
    """
    if payload:
        try:
            request = WalkthroughUpdateRequest.model_validate_json(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid payload JSON: {exc}")
    else:
        request = WalkthroughUpdateRequest()

    incoming: list[tuple[str, bytes, str]] = []
    for upload in screenshots or []:
        if upload is None or not upload.filename:
            continue
        mime = (upload.content_type or "").lower()
        if mime not in _WALKTHROUGH_ALLOWED_IMAGE_MIME:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported attachment type: {mime or 'unknown'}. "
                    "Allowed: PNG, JPEG, GIF, WEBP, PDF."
                ),
            )
        content = await upload.read()
        if len(content) > _WALKTHROUGH_MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"{upload.filename} is larger than 10 MB.",
            )
        incoming.append((upload.filename, content, mime))

    uploaded_refs: list[dict] = []
    if incoming:
        jira = JiraClient()
        try:
            uploaded = await jira.upload_attachments(ticket_key, incoming)
        except JiraNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except JiraAuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
        except JiraConnectionError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        # Resolve each fresh upload's media-services UUID so future
        # Pass-to-UAT comments can render the screenshot inline. UUID
        # lookup failures don't block the save — the walkthrough still
        # persists with media_id=None and comment rendering falls back
        # to the `📷 <filename>` text callout.
        enriched = await jira.enrich_attachments_with_media_ids(uploaded)
        # Rebuild the persisted records — enrich_attachments_with_media_ids
        # already applies the same filename fallback and drops entries
        # without a content URL.
        for image in enriched:
            record: dict = {"filename": image.filename, "url": image.url}
            if image.media_id:
                record["media_id"] = image.media_id
            uploaded_refs.append(record)

    final_list: list[dict] = []
    for ref in request.existing_screenshots:
        if not ref.url or not ref.url.strip():
            continue
        record: dict = {
            "filename": (ref.filename or "screenshot").strip() or "screenshot",
            "url": ref.url.strip(),
        }
        if ref.media_id and ref.media_id.strip():
            record["media_id"] = ref.media_id.strip()
        final_list.append(record)
    final_list.extend(uploaded_refs)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await walkthrough_repository.upsert_walkthrough(
            session,
            ticket_key=ticket_key,
            loom_url=request.loom_url,
            notes=request.notes,
            screenshots=final_list,
        )
        return await uat_readiness.fetch_readiness(session, ticket_key=ticket_key)


def _serialize_progress(row) -> dict:
    """Shape a TestPlanProgress row (or None) into the JSON the frontend expects."""
    if row is None:
        return {"checked_ids": [], "updated_at": None}
    try:
        checked = json.loads(row.checked_ids) if row.checked_ids else []
    except (ValueError, TypeError):
        checked = []
    return {
        "checked_ids": checked if isinstance(checked, list) else [],
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@app.get("/test-plan-progress/{progress_key}")
async def get_test_plan_progress(progress_key: str):
    """Return the shared, per-ticket set of checked test cases for a plan.

    ``progress_key`` is the composite the frontend builds from the ticket key(s)
    plus a fingerprint of the plan's section sizes; progress is shared across
    everyone testing the ticket and resets when a regenerated plan changes shape.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await test_plan_progress_repository.get_progress(
            session, progress_key=progress_key
        )
        return _serialize_progress(row)


@app.put("/test-plan-progress/{progress_key}")
async def put_test_plan_progress(
    progress_key: str, request: TestPlanProgressUpdateRequest
):
    """Create or replace the shared checked-case set for a plan. The client sends
    the full set each save, so an empty list clears all checks."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await test_plan_progress_repository.upsert_progress(
            session,
            progress_key=progress_key,
            checked_ids=request.checked_ids,
        )
        return _serialize_progress(row)


def _serialize_hold(row) -> dict:
    """Shape a TicketHold row (or None) into the JSON the frontend expects.

    ``held`` is explicit rather than implied by the other fields so the client
    never has to guess from a null reason.
    """
    if row is None:
        return {"held": False, "reason": None, "note": None, "held_since": None}
    return {
        "held": True,
        "reason": row.reason,
        "note": row.note,
        "held_since": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@app.get("/tickets/{ticket_key}/hold")
async def get_ticket_hold(ticket_key: str):
    """Return the ticket's shared QA hold, or ``held: false`` when it's active.

    A hold means "testing is parked and here's why" — separate from Jira status
    and from Jira's blocked-by links, and shared across everyone testing it.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await ticket_hold_repository.get_hold(session, ticket_key=ticket_key)
        return _serialize_hold(row)


@app.put("/tickets/{ticket_key}/hold")
async def put_ticket_hold(ticket_key: str, request: TicketHoldRequest):
    """Put the ticket on QA hold, or edit the reason/note of an existing hold."""
    reason = (request.reason or "").strip().lower()
    if reason not in HOLD_REASONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown hold reason '{request.reason}'. Expected one of: "
            + ", ".join(sorted(HOLD_REASONS)),
        )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await ticket_hold_repository.upsert_hold(
            session,
            ticket_key=ticket_key,
            reason=reason,
            note=request.note,
        )
        return _serialize_hold(row)


@app.delete("/tickets/{ticket_key}/hold")
async def delete_ticket_hold(ticket_key: str):
    """Resume the ticket. Idempotent — clearing a ticket that isn't held is fine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await ticket_hold_repository.clear_hold(session, ticket_key=ticket_key)
        return _serialize_hold(None)
