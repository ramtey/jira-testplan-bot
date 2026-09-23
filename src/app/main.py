import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .attachment_types import (
    ALLOWED_ATTACHMENT_LABEL,
    MAX_ATTACHMENT_BYTES,
    resolve_attachment_mime,
)
from .bug_lens_routes import router as bug_lens_router
from .config import settings
from .db import crud
from .db.models.run import Run as RunModel
from .db.models.ticket_hold import HOLD_REASONS
from .db.mongo import ensure_indexes, get_db
from .jira_client import (
    JiraAuthError,
    JiraClient,
    JiraConnectionError,
    JiraContentLimitError,
    JiraNotFoundError,
    plan_version_note,
)
from .llm_client import LLMError, get_llm_client
from .models import (
    AdoptPlanRequest,
    GenerateTestPlanRequest,
    MarkCarryOverRequest,
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
from .services import plan_adoption, plan_service
from .services import mark_carryover
from .services import progress_key as progress_key_service
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


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Create the collection indexes on boot.

    This is what replaces `alembic upgrade head`. Mongo creates collections
    lazily on first write, so there is no schema to migrate — but the unique
    indexes on ticket_key/progress_key are load-bearing: several repositories
    upsert on the assumption that at most one document matches. Index creation
    is idempotent, so re-running on every boot is free.
    """
    try:
        await ensure_indexes()
    except Exception:
        # A bad URI or an unreachable cluster should surface on the first real
        # request with a clear driver error, not abort startup — the frontend
        # and the Jira-only routes still work without the database.
        logger.exception("Mongo index setup failed; continuing without it")
    yield


app = FastAPI(title="Jira Test Plan Bot", version="0.1.0", lifespan=lifespan)
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


# A health check that cannot fail reports nothing. This one had been
# `return {"status": "ok"}` — a literal — so on 2026-09-18 it answered "ok"
# while Atlas was unreachable and every database-backed route was returning
# 503. Same shape as the Figma check that passed through a spent quota:
# the endpoint whose whole job is to report a failure was the one hiding it.
_HEALTH_PING_TIMEOUT_SECONDS = 3.0


@app.get("/health")
async def health(response: Response):
    """Liveness plus a real database round-trip.

    Returns 503 when Mongo cannot be reached, because every route that
    matters is dead in that state and a 200 here would send a caller
    looking for the problem somewhere else. The ping is bounded well under
    the client's own 10s server-selection timeout: a health check that
    hangs for ten seconds is its own kind of failure, and an unreachable
    cluster should be *reported* quickly, not waited out.
    """
    database: dict[str, str] = {"status": "ok"}
    try:
        await asyncio.wait_for(
            get_db().command("ping"), timeout=_HEALTH_PING_TIMEOUT_SECONDS
        )
    except TimeoutError:
        database = {
            "status": "unreachable",
            "error": f"ping exceeded {_HEALTH_PING_TIMEOUT_SECONDS:g}s",
        }
    except Exception as e:
        # Includes the case where the client cannot even be constructed —
        # a missing MONGODB_URI is as fatal to the app as a dead cluster.
        database = {"status": "unreachable", "error": f"{type(e).__name__}: {e}"[:300]}

    if database["status"] != "ok":
        response.status_code = 503
        logger.warning("health: database unreachable — %s", database["error"])
        return {"status": "degraded", "database": database}

    return {"status": "ok", "database": database}


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
        # Read the version before posting, because the version rides on the
        # comment's marker line. Posting a regeneration edits the existing
        # comment, which leaves Jira's `created` at the original date and moves
        # only `updated` — a watcher sees no new activity and can conclude the
        # regeneration failed (SK-2325, comments 332346/332347). The marker is
        # the one line that stays outside the collapsed body, so a version
        # number there is the difference between "this is the plan I already
        # read" and "this is v3".
        #
        # A version we cannot read costs the note, not the post: the comment is
        # still correct without it, and `version_note` in the response says
        # whether one was written rather than leaving the client to assume.
        version_note: str | None = None
        if request.plan_id is not None:
            try:
                existing = await plan_repository.get_plan_with_cases(
                    get_db(), plan_id=request.plan_id
                )
                if existing:
                    version_note = plan_version_note(existing[0].version)
            except Exception:
                logging.exception(
                    "Could not read plan %s to version its Jira comment",
                    request.plan_id,
                )

        result = await jira.post_comment(
            request.issue_key, request.comment_text, version_note=version_note
        )
        comment_id = result.get("id")
        posted_at_iso: str | None = None
        # Tri-state. None: there was no plan to record (no plan_id — the client
        # already says "not tracked" for that). True: this version is now marked
        # as the one live in Jira. False: the comment landed but the mark did
        # not — the plan is on the ticket while the version badge still reads
        # "Not live in Jira", and re-posting repeats it identically because the
        # write fails the same way every time. That third case is the silent
        # success this endpoint keeps being fixed for; it is reported, not
        # swallowed.
        recorded: bool | None = None
        record_error: str | None = None
        if request.plan_id is not None:
            if not comment_id:
                recorded = False
                record_error = "Jira did not return a comment id"
            else:
                db = None
                try:
                    db = get_db()
                    await plan_repository.mark_plan_posted_to_jira(
                        db,
                        plan_id=request.plan_id,
                        ticket_key=request.issue_key.upper(),
                        jira_comment_id=str(comment_id),
                    )
                    recorded = True
                except Exception:
                    # Posting succeeded; failing to record the mark shouldn't
                    # fail the request. The tester keeps their posted plan —
                    # they are told the app could not record it.
                    recorded = False
                    record_error = "the app could not reach its database"
                    logging.exception(
                        "Failed to mark plan %s posted on %s",
                        request.plan_id,
                        request.issue_key,
                    )
                if recorded:
                    # Read-back only, for the timestamp. The mark is what makes
                    # this version live; losing its timestamp here does not
                    # unmake it, so this failure must never flip `recorded`.
                    try:
                        plan_with_cases = await plan_repository.get_plan_with_cases(
                            db, plan_id=request.plan_id
                        )
                        if plan_with_cases and plan_with_cases[0].posted_at:
                            posted_at_iso = plan_with_cases[0].posted_at.isoformat()
                    except Exception:
                        logging.exception(
                            "Marked plan %s posted on %s but could not read it back",
                            request.plan_id,
                            request.issue_key,
                        )
        return {
            "success": True,
            "comment_id": comment_id,
            "issue_key": request.issue_key,
            "updated": result.get("updated", False),
            "truncated": result.get("truncated", False),
            # A plan too big for one Jira comment is posted across several. The
            # response is an explicit allowlist, so every field the client needs
            # to describe a partial or split post has to be named here — left
            # out, they default to "one comment, all of it landed", which is the
            # silent success this endpoint keeps being fixed for.
            "parts": result.get("parts", 1),
            "posted_parts": result.get("posted_parts", 1),
            "part_comment_ids": result.get("part_comment_ids", []),
            "stale_parts_left": result.get("stale_parts_left", 0),
            "part_error": result.get("part_error"),
            # The version line written onto the comment's marker, or null when
            # the plan's version could not be read. Named so the client can say
            # which version is live on the ticket rather than inferring it.
            "version_note": result.get("version_note"),
            "plan_id": request.plan_id,
            # Null when the plan was never persisted (no plan_id), so the client
            # can say "not tracked" rather than quietly showing no status at all.
            "posted_at": posted_at_iso,
            # Whether this version was recorded as the one live in Jira. False
            # means the comment is on the ticket but the version badge will not
            # reflect it — the client has to say so, because retrying does not
            # help while the cause persists. `record_error` is a reason fragment
            # ("the app could not reach its database"), not a sentence: the
            # client composes it into one.
            "recorded": recorded,
            "record_error": record_error,
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
        # Catch-all for unexpected errors. `logging` is the module-level import
        # — a local `import logging` here would make the name local to the whole
        # function and shadow it in the handlers above, which are the ones that
        # run while this endpoint is degrading rather than failing.
        logging.exception(f"Unexpected error posting comment to {request.issue_key}: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while posting the comment"
        )


# Walkthrough serialization + readiness composition lives in
# ``src.app.uat_readiness`` so the workflow route (which enforces the
# ``needs_walkthrough`` rule server-side) computes it identically.


@app.get("/tickets/{ticket_key}/walkthrough")
async def get_ticket_walkthrough(ticket_key: str):
    """Return the human-authored walkthrough (Loom link, screenshot, notes) for a
    ticket. Kept apart from the generated plan so it survives regeneration.

    Also echoes the ticket's latest known ``uat_complexity`` (from the most
    recent generated plan) so the workflow UI can decide whether to nudge for a
    walkthrough when the ticket is passed to UAT.
    """
    db = get_db()
    return await uat_readiness.fetch_readiness(db, ticket_key=ticket_key)


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
        mime = resolve_attachment_mime(upload.filename, upload.content_type)
        if mime is None:
            declared = (upload.content_type or "").strip() or "unknown"
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported attachment type: {declared}. "
                    f"Allowed: {ALLOWED_ATTACHMENT_LABEL}."
                ),
            )
        content = await upload.read()
        if len(content) > MAX_ATTACHMENT_BYTES:
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

    db = get_db()
    row = await walkthrough_repository.upsert_walkthrough(
        db,
        ticket_key=ticket_key,
        loom_url=request.loom_url,
        notes=request.notes,
        screenshots=final_list,
    )
    return await uat_readiness.fetch_readiness(db, ticket_key=ticket_key)


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

    An unknown key is a 404, not an empty set. It used to return
    ``{"checked_ids": []}`` for any string at all, which made a mistyped or
    stale key indistinguishable from a real plan nobody has checked yet — so
    ``mark-passed.sh``, whose stated guard is "a non-200 GET means the key is
    wrong", could never detect one and wrote QA results somewhere nothing reads.
    Callers that legitimately expect "no progress yet" treat 404 as empty; the
    frontend already falls back to its local state when the fetch fails.
    """
    db = get_db()
    row = await test_plan_progress_repository.get_progress(
        db, progress_key=progress_key
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No progress recorded for key {progress_key!r}",
        )
    return _serialize_progress(row)


@app.get("/test-plan-progress/{progress_key}/other-shapes")
async def get_progress_under_other_shapes(progress_key: str):
    """Progress recorded for the same ticket(s) under a *different* plan shape.

    The fingerprint in a progress key is the plan's section sizes, so any
    regeneration that changes those sizes moves progress to a fresh key. That is
    deliberate — stale checks must not carry over onto a different set of cases —
    but it left the old marks invisible: the UI polls the current shape, gets the
    404 above, and renders 0%, which reads as "nobody tested this" rather than
    "your results are filed under the plan they were made against".

    This is the lookup that tells those two apart. Read-only, and it never
    migrates anything: which checks still apply after a regeneration is a
    judgement only a tester can make.
    """
    db = get_db()
    rows = await test_plan_progress_repository.find_for_same_tickets(
        db, progress_key=progress_key
    )
    asked = progress_key.upper()
    others = []
    for row in rows:
        if row.progress_key == asked:
            continue
        try:
            checked = json.loads(row.checked_ids)
        except (ValueError, TypeError):
            checked = []
        others.append(
            {
                "progress_key": row.progress_key,
                "fingerprint": row.progress_key.rsplit(":", 1)[-1],
                "checked_count": len(checked) if isinstance(checked, list) else 0,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )
    return {"progress_key": asked, "others": others}


@app.get("/plans/{plan_id}/progress-key")
async def get_plan_progress_key(plan_id: int):
    """The canonical progress key for a stored plan.

    Exists so the key has exactly one producer. Anything writing progress for a
    plan — the UAT runner above all — should ask for the key rather than derive
    it, because the derivation has to drop cases flagged ``covered_by_unit_test``
    and that rule is easy to miss when counting sections by hand.
    """
    db = get_db()
    result = await plan_repository.get_plan_with_cases(db, plan_id=plan_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    plan, _cases = result
    run = await crud.get_by_id(db, RunModel, plan.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Plan has no run")
    key = progress_key_service.build_progress_key(list(run.ticket_keys or []), plan.body)
    return {
        "plan_id": plan.id,
        "ticket_keys": list(run.ticket_keys or []),
        "progress_key": key,
        "fingerprint": progress_key_service.fingerprint(plan.body),
        # Cases the planner flagged as already unit-tested. They are optional —
        # excluded from the checklist denominator — but addressable, under
        # `covered_by_unit_test:<n>`. Named here so a caller can size that
        # namespace without re-deriving the rule that produces it.
        "covered_count": progress_key_service.covered_length(plan.body),
        # The full id space, so anything writing progress can validate an id
        # against the plan instead of against a count it worked out by hand.
        "case_ids": list(progress_key_service.case_index(plan.body)),
    }


@app.get("/plans/{plan_id}/mark-carryover")
async def get_mark_carryover(plan_id: int):
    """What the tester ticked on the previous shape of this plan, and where each
    mark lands now.

    A proposal, not a migration. Regenerating moves progress to a new key by
    design, and `other-shapes` could already say the old marks existed while
    refusing to move them — which left the tester re-mapping sixteen checks
    against two Jira comments in two tabs (SK-2325, plan 532 to 536). This names
    each old mark, the case it matches now, and whether the wording changed;
    `POST` carries over only what comes back.
    """
    db = get_db()
    result = await mark_carryover.build(db, plan_id=plan_id)
    if result.get("error") == "plan_not_found":
        raise HTTPException(status_code=404, detail="Plan not found")
    if result.get("error") == "run_not_found":
        raise HTTPException(status_code=404, detail="Plan has no run")
    return result


@app.post("/plans/{plan_id}/mark-carryover")
async def post_mark_carryover(plan_id: int, request: MarkCarryOverRequest):
    """Union the confirmed carry-over ids into this plan's progress.

    The ids are the *new* plan's, and every one is checked against the plan's
    own case index before anything is written. An id naming no case is a 422,
    not a stored row: a check the UI reads and matches against nothing is the
    exact invisible-progress failure this area keeps being fixed for.
    """
    db = get_db()
    result = await mark_carryover.apply(db, plan_id=plan_id, ids=request.ids)
    if result.get("error") == "plan_not_found":
        raise HTTPException(status_code=404, detail="Plan not found")
    if result.get("error") == "run_not_found":
        raise HTTPException(status_code=404, detail="Plan has no run")
    if result.get("error") == "unknown_ids":
        raise HTTPException(
            status_code=422,
            detail=(
                "These ids name no case in this plan: "
                + ", ".join(result["unknown"])
            ),
        )
    return result


@app.post("/tickets/{ticket_key}/adopt-plan")
async def adopt_plan_from_jira(ticket_key: str, request: AdoptPlanRequest):
    """Register a plan that exists only as a Jira comment as a run + plan.

    The recovery path for a plan the database never recorded — generation ran,
    the plan was posted to the ticket, and ``start_run`` had already swallowed a
    database error, so no ``plan_id`` was ever produced. Without a run and a plan
    body there is no ``build_progress_key`` result to write UAT progress under,
    so the runner cannot mark and the UI renders nothing.

    Deliberately *not* a regeneration: ``generate_single`` would produce a
    different plan from the reviewed one already on the ticket. This parses the
    reviewed plan back out of its own comment.

    Two-step by default. ``confirm: false`` (the default) parses and reports the
    section counts and the derived key without writing anything; ``confirm:
    true`` persists. The preview exists because a misparsed section count
    produces a key the UI never polls — progress written into a void, which is
    the exact failure ``progress_key`` was created to end.
    """
    try:
        if request.confirm:
            return await plan_adoption.commit(ticket_key, request.comment_id)
        return await plan_adoption.preview(ticket_key, request.comment_id)
    except plan_adoption.PlanAdoptionError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except JiraNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except JiraAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except JiraConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.put("/test-plan-progress/{progress_key}")
async def put_test_plan_progress(
    progress_key: str, request: TestPlanProgressUpdateRequest
):
    """Create or replace the shared checked-case set for a plan. The client sends
    the full set each save, so an empty list clears all checks."""
    db = get_db()
    row = await test_plan_progress_repository.upsert_progress(
        db,
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
    db = get_db()
    row = await ticket_hold_repository.get_hold(db, ticket_key=ticket_key)
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
    db = get_db()
    row = await ticket_hold_repository.upsert_hold(
        db,
        ticket_key=ticket_key,
        reason=reason,
        note=request.note,
    )
    return _serialize_hold(row)


@app.delete("/tickets/{ticket_key}/hold")
async def delete_ticket_hold(ticket_key: str):
    """Resume the ticket. Idempotent — clearing a ticket that isn't held is fine."""
    db = get_db()
    await ticket_hold_repository.clear_hold(db, ticket_key=ticket_key)
    return _serialize_hold(None)
