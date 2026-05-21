import json
import os
from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .bug_lens_routes import router as bug_lens_router
from .config import NON_TESTABLE_ISSUE_TYPES
from .db.models.plan import PlanFormat
from .description_analyzer import extract_acceptance_criteria
from .db.models.run import RunType
from .db.session import get_sessionmaker
from .jira_client import (
    JiraAuthError,
    JiraClient,
    JiraConnectionError,
    JiraNotFoundError,
    is_blocked_bot_display_name,
)
from .llm_client import LLMError, get_llm_client
from .models import (
    GenerateTestPlanRequest,
    MultiTicketGenerateRequest,
    PostCommentRequest,
    TicketInput,
    WorkflowActionRequest,
)
from .repositories import bug_analysis_repository
from .runs_routes import router as runs_router
from .services import run_tracker
from .slack_client import resolve_slack_messages_in_text
from .token_service import token_health_service


def _derive_context_flags(request: GenerateTestPlanRequest | TicketInput) -> dict:
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


def _normalize_grounding_warnings(test_plan, valid_ac_ids: set[str] | None = None) -> list[dict]:
    """Sanitize the LLM-returned ``grounding_warnings`` so the UI can render
    them safely.

    The model is asked to flag every UI element it referenced in a test step
    but couldn't trace back to the PR diff, testID reference, or attached
    mockups. We validate the shape (dict with three non-empty string fields),
    strip whitespace, and dedupe entries that point at the same
    ``(ac_id, missing_element)``.

    For multi-ticket plans, ``valid_ac_ids`` should contain every legal
    ``<ticket>-AC<n>`` ID; entries with an unrecognized ``ac_id`` are
    discarded — they're almost always a sign the model paraphrased the AC
    instead of citing it. For single-ticket plans, leave ``valid_ac_ids`` as
    ``None`` to skip that check (there's no canonical ID format there).
    """
    raw = getattr(test_plan, "grounding_warnings", None) or []
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        ac_id = (entry.get("ac_id") or "").strip()
        missing = (entry.get("missing_element") or "").strip()
        explanation = (entry.get("explanation") or "").strip()
        if not ac_id or not missing or not explanation:
            continue
        if valid_ac_ids is not None and ac_id not in valid_ac_ids:
            continue
        dedupe_key = (ac_id, missing.lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        out.append({
            "ac_id": ac_id,
            "missing_element": missing,
            "explanation": explanation,
        })
    return out


def _compute_ac_coverage(test_plan, tickets_data: list[dict]) -> dict:
    """Compare AC IDs declared on each test case (`covers_acs`) against the
    flat AC index built from the request.

    Side effect: strips any AC IDs from each test case's ``covers_acs`` that
    aren't in the request's index. The LLM occasionally invents or mis-numbers
    IDs ("SK-2138-AC9" when only 8 ACs exist, or tagging an unrelated ticket's
    AC); leaving those in the response would inflate coverage in the UI and
    show fake tags on test cases. Invalid IDs are surfaced separately so the
    UI can flag the regression instead of hiding it.

    Multi-ticket only: if the LLM reported ``superseded_acs`` (older ACs
    overridden by a newer ticket's AC), those loser IDs are excluded from
    the per-ticket "uncovered" list — they're intentionally not tested,
    not gaps. They're surfaced as their own top-level array so the UI
    can show the override and the reason.

    Returns a structure the frontend can render directly:
        {
            "tickets": {
                "SK-2137": {
                    "covered": ["SK-2137-AC1", "SK-2137-AC2"],
                    "uncovered": [
                        {"id": "SK-2137-AC3", "text": "..."},
                    ],
                    "superseded": [
                        {"id": "SK-2138-AC3", "text": "...", "winner_id": "SK-2194-AC1"},
                    ],
                    "total": 4,
                },
                ...
            },
            "uncovered_total": 3,
            "invalid_ids": ["SK-2138-AC9"],  # IDs the LLM made up
            "superseded_acs": [
                {"loser_id": "SK-2138-AC3", "loser_text": "...",
                 "loser_ticket": "SK-2138", "winner_id": "SK-2194-AC1",
                 "winner_text": "...", "winner_ticket": "SK-2194",
                 "reason": "..."},
            ],
        }
    """
    per_ticket: dict[str, list[tuple[str, str]]] = {}
    for ticket in tickets_data:
        key = ticket["ticket_key"]
        acs = ticket.get("acceptance_criteria") or []
        per_ticket[key] = [(f"{key}-AC{i}", text) for i, text in enumerate(acs, 1)]

    valid_ids: set[str] = {ac_id for entries in per_ticket.values() for ac_id, _ in entries}
    id_to_text: dict[str, str] = {
        ac_id: text for entries in per_ticket.values() for ac_id, text in entries
    }

    def _ticket_of(ac_id: str) -> str:
        # "SK-2138-AC3" → "SK-2138"
        return ac_id.rsplit("-AC", 1)[0] if "-AC" in ac_id else ""

    # ── Validate superseded_acs from the LLM ─────────────────────────────
    raw_superseded = getattr(test_plan, "superseded_acs", None) or []
    superseded_pairs: list[dict] = []
    superseded_loser_ids: set[str] = set()
    seen_losers: set[str] = set()
    for entry in raw_superseded:
        if not isinstance(entry, dict):
            continue
        loser = (entry.get("loser_id") or "").strip()
        winner = (entry.get("winner_id") or "").strip()
        reason = (entry.get("reason") or "").strip()
        if not loser or not winner or loser == winner:
            continue
        if loser not in valid_ids or winner not in valid_ids:
            continue
        if loser in seen_losers:
            continue  # one supersede per loser; first one wins
        seen_losers.add(loser)
        loser_ticket = _ticket_of(loser)
        winner_ticket = _ticket_of(winner)
        # Sanity: winner must come from a strictly newer ticket than loser.
        # If the LLM got recency backwards, drop the entry — better to leave
        # the AC as "uncovered" than to silently honour a wrong override.
        from .llm_client import _ticket_key_recency
        if _ticket_key_recency(winner_ticket) <= _ticket_key_recency(loser_ticket):
            continue
        superseded_loser_ids.add(loser)
        superseded_pairs.append({
            "loser_id": loser,
            "loser_text": id_to_text.get(loser, ""),
            "loser_ticket": loser_ticket,
            "winner_id": winner,
            "winner_text": id_to_text.get(winner, ""),
            "winner_ticket": winner_ticket,
            "reason": reason,
        })

    declared: set[str] = set()
    invalid_ids: set[str] = set()
    for bucket in (test_plan.happy_path, test_plan.edge_cases, test_plan.integration_tests):
        for case in bucket or []:
            if not isinstance(case, dict):
                continue
            raw = case.get("covers_acs") or []
            if not isinstance(raw, list):
                continue
            kept: list[str] = []
            for ac_id in raw:
                if not isinstance(ac_id, str):
                    continue
                trimmed = ac_id.strip()
                if not trimmed:
                    continue
                if trimmed in valid_ids:
                    # Drop any test-case tag pointing at a superseded AC — the
                    # newer AC is the source of truth, and leaving the old ID
                    # here would mislead the UI into showing it as "covered".
                    if trimmed in superseded_loser_ids:
                        continue
                    declared.add(trimmed)
                    kept.append(trimmed)
                else:
                    invalid_ids.add(trimmed)
            # Rewrite the case so the UI / persisted plan only show real IDs.
            case["covers_acs"] = kept

    result_tickets: dict[str, dict] = {}
    uncovered_total = 0
    winner_by_loser = {p["loser_id"]: p["winner_id"] for p in superseded_pairs}
    for key, entries in per_ticket.items():
        covered: list[str] = []
        uncovered: list[dict] = []
        superseded: list[dict] = []
        for ac_id, text in entries:
            if ac_id in superseded_loser_ids:
                superseded.append({
                    "id": ac_id,
                    "text": text,
                    "winner_id": winner_by_loser[ac_id],
                })
            elif ac_id in declared:
                covered.append(ac_id)
            else:
                uncovered.append({"id": ac_id, "text": text})
        uncovered_total += len(uncovered)
        result_tickets[key] = {
            "covered": covered,
            "uncovered": uncovered,
            "superseded": superseded,
            # `total` excludes superseded ACs so the X/Y ratio in the UI
            # reflects what was actually expected to be tested.
            "total": len(entries) - len(superseded),
        }
    return {
        "tickets": result_tickets,
        "uncovered_total": uncovered_total,
        "invalid_ids": sorted(invalid_ids),
        "superseded_acs": superseded_pairs,
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
        "workflow_project_prefixes": settings.workflow_project_prefixes,
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

        # Serialize children (direct sub-tasks / Epic children) if present.
        # The frontend echoes these back to /generate-test-plan so the prompt
        # can switch into parent/integration-test mode.
        children_list = None
        if issue.children:
            children_list = [asdict(child) for child in issue.children]

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

        bounce_history_list = None
        if issue.bounce_history:
            bounce_history_list = [asdict(b) for b in issue.bounce_history]

        # Best-effort: include the bot user's accountId so the frontend
        # can filter self-mentions out of the Notify picker. Cached in the
        # JiraClient class after first call. Any failure (network, auth,
        # malformed /myself response) is non-fatal — the picker just keeps
        # the self entry instead.
        current_user_account_id = None
        try:
            current_user_account_id = await jira.get_my_account_id()
        except Exception:
            pass

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
            "development_info": development_info_dict,
            "attachments": attachments_list,
            "comments": comments_list,
            "parent": parent_info_dict,
            "children": children_list,
            "linked_issues": linked_info_dict,
            "status": issue.status,
            "status_category": issue.status_category,
            "bounce_history": bounce_history_list,
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
async def list_jira_projects():
    """List Jira projects accessible to the configured account."""
    jira = JiraClient()
    try:
        projects = await jira.list_projects()
        return {"projects": projects}
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


# QA workflow actions for the SK project. Hardcoded for SK only — generalize
# to other projects via a per-project config once a second project needs it.
SK_WORKFLOW_ACTIONS: dict[str, str] = {
    "pull-to-testing": "In Testing",
    "pass-to-uat": "Ready for UAT",
    "fail-to-todo": "To Do",
}


@app.post("/issue/{issue_key}/workflow/{action}")
async def run_workflow_action(
    issue_key: str,
    action: str,
    payload: WorkflowActionRequest | None = None,
):
    """Execute a single-click QA workflow action: transition + reassign."""
    if not issue_key.upper().startswith("SK-"):
        raise HTTPException(
            status_code=400,
            detail="Workflow actions are only enabled for the SK project right now.",
        )
    if action not in SK_WORKFLOW_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    target_status = SK_WORKFLOW_ACTIONS[action]
    jira = JiraClient()

    try:
        transitions = await jira.list_transitions(issue_key)
        transition = next(
            (
                t for t in transitions
                if (t.get("to") or {}).get("name", "").strip().lower()
                == target_status.lower()
            ),
            None,
        )
        if transition is None:
            available = sorted({
                (t.get("to") or {}).get("name") for t in transitions
                if (t.get("to") or {}).get("name")
            })
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No transition to '{target_status}' is available from the "
                    f"current status. Available transitions: {available or 'none'}."
                ),
            )

        my_account_id = await jira.get_my_account_id()
        if action == "pull-to-testing":
            target_account_id = my_account_id
            assigned_label = "you"
            resolved_via = "self"
        else:
            # Exclude the bot's own account from both lookups: pull-to-testing
            # always parks the ticket on the bot, so the bot showing up as a
            # prior `from` (or as a loose name match in PR-contributor search)
            # is noise, not a real developer to hand the ticket back to.
            target_account_id, prior_name = await jira.get_prior_assignee_account_id(
                issue_key, exclude_account_id=my_account_id
            )
            if target_account_id:
                assigned_label = prior_name or "prior assignee"
                resolved_via = "prior-assignee"
            else:
                target_account_id, contributor_name = await jira.get_top_pr_contributor_account_id(
                    issue_key, exclude_account_id=my_account_id
                )
                if target_account_id:
                    assigned_label = contributor_name or "top contributor"
                    resolved_via = "pr-contributor"
                else:
                    assigned_label = "unassigned"
                    resolved_via = "unassigned"

            # Final safety net: if anything upstream slipped through and
            # resolved to a known bot (by accountId or display name), treat
            # it as "no real developer found" and unassign instead of
            # bouncing the ticket back to the bot.
            if (
                target_account_id == my_account_id
                or is_blocked_bot_display_name(assigned_label)
            ):
                target_account_id = None
                assigned_label = "unassigned"
                resolved_via = "unassigned-safety-net"

        import logging
        logging.info(
            "Workflow %s on %s: resolved assignee via %s -> %s",
            action,
            issue_key,
            resolved_via,
            assigned_label,
        )

        await jira.transition_issue(issue_key, transition["id"])
        await jira.assign_issue(issue_key, target_account_id)

        comment_posted = False
        if action == "pass-to-uat" and payload is not None:
            try:
                result = await jira.post_qa_pass_comment(
                    issue_key,
                    payload.loom_urls,
                    payload.summary,
                    payload.environments,
                    payload.mention_account_ids,
                    payload.image_urls,
                )
                comment_posted = result is not None
            except (JiraNotFoundError, JiraAuthError, JiraConnectionError) as exc:
                # Transition + reassign already succeeded — surface the
                # comment failure but don't roll back the workflow move.
                logging.warning(
                    "pass-to-uat comment failed on %s: %s", issue_key, exc
                )
        elif action == "fail-to-todo" and payload is not None:
            try:
                result = await jira.post_qa_fail_comment(
                    issue_key,
                    payload.reason,
                    payload.loom_urls,
                    payload.image_urls,
                    payload.mention_account_ids,
                )
                comment_posted = result is not None
            except (JiraNotFoundError, JiraAuthError, JiraConnectionError) as exc:
                logging.warning(
                    "fail-to-todo comment failed on %s: %s", issue_key, exc
                )

        parent_transitioned = False
        parent_key: str | None = None
        if action == "pass-to-uat":
            parent_transitioned, parent_key = await _maybe_transition_parent_to_uat(
                jira, issue_key, target_status
            )

        cascaded_subtasks: list[str] = []
        if payload is not None and payload.cascade_to_subtasks:
            cascaded_subtasks = await _cascade_transition_to_subtasks(
                jira, issue_key, target_status
            )

        return {
            "status": "ok",
            "action": action,
            "target_status": target_status,
            "assigned_to": assigned_label,
            "comment_posted": comment_posted,
            "parent_transitioned": parent_transitioned,
            "parent_key": parent_key if parent_transitioned else None,
            "cascaded_subtasks": cascaded_subtasks,
        }
    except JiraNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except JiraAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except JiraConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))


async def _maybe_transition_parent_to_uat(
    jira: JiraClient, subtask_key: str, target_status: str
) -> tuple[bool, str | None]:
    """If `subtask_key`'s siblings are all at-or-past UAT, move the parent too.

    Best-effort: any failure is swallowed (logged) so the primary subtask
    transition still reports success. Skipped when the parent is an Epic —
    Epics don't auto-roll up on a child's UAT handoff.
    """
    import logging

    target_lower = target_status.strip().lower()

    def _satisfies(subtask: dict) -> bool:
        status = ((subtask.get("fields") or {}).get("status") or {})
        name = (status.get("name") or "").strip().lower()
        category = (status.get("statusCategory") or {}).get("key", "")
        return name == target_lower or category == "done"

    try:
        info = await jira.get_sibling_subtasks_info(subtask_key)
        if not info:
            return False, None
        if (info.get("parent_issue_type") or "").strip().lower() == "epic":
            return False, None
        siblings = info.get("subtasks") or []
        if not siblings or not all(_satisfies(s) for s in siblings):
            return False, None

        parent_key = info["parent_key"]
        parent_transitions = await jira.list_transitions(parent_key)
        transition = next(
            (
                t for t in parent_transitions
                if (t.get("to") or {}).get("name", "").strip().lower() == target_lower
            ),
            None,
        )
        if transition is None:
            logging.info(
                "Skipping parent auto-transition: parent %s has no '%s' transition available",
                parent_key,
                target_status,
            )
            return False, parent_key

        await jira.transition_issue(parent_key, transition["id"])
        logging.info(
            "Auto-transitioned parent %s to %s after last subtask %s passed to UAT",
            parent_key,
            target_status,
            subtask_key,
        )
        return True, parent_key
    except (JiraNotFoundError, JiraAuthError, JiraConnectionError) as exc:
        logging.warning(
            "Parent auto-transition failed for %s: %s", subtask_key, exc
        )
        return False, None


async def _cascade_transition_to_subtasks(
    jira: JiraClient, parent_key: str, target_status: str
) -> list[str]:
    """Transition every direct subtask of `parent_key` to `target_status`.

    Each subtask is moved via its own transition whose `to.name` matches
    `target_status` (case-insensitive). Subtasks already at the target are
    skipped; subtasks whose workflow has no matching transition are skipped
    silently — per user preference, we don't surface partial failures.
    Returns the list of subtask keys that were actually moved.
    """
    import logging

    moved: list[str] = []
    target_lower = target_status.strip().lower()

    try:
        subtasks = await jira.get_subtasks_of(parent_key)
    except (JiraNotFoundError, JiraAuthError, JiraConnectionError) as exc:
        logging.warning(
            "Subtask cascade aborted for %s: failed to fetch subtasks (%s)",
            parent_key,
            exc,
        )
        return moved

    for sub in subtasks:
        sub_key = sub.get("key")
        if not sub_key:
            continue
        status_name = (
            ((sub.get("fields") or {}).get("status") or {}).get("name") or ""
        ).strip().lower()
        if status_name == target_lower:
            continue
        try:
            transitions = await jira.list_transitions(sub_key)
            transition = next(
                (
                    t for t in transitions
                    if (t.get("to") or {}).get("name", "").strip().lower()
                    == target_lower
                ),
                None,
            )
            if transition is None:
                logging.info(
                    "Cascade skip: subtask %s has no transition to '%s'",
                    sub_key,
                    target_status,
                )
                continue
            await jira.transition_issue(sub_key, transition["id"])
            moved.append(sub_key)
        except (JiraNotFoundError, JiraAuthError, JiraConnectionError) as exc:
            logging.warning(
                "Cascade transition failed for %s: %s", sub_key, exc
            )

    return moved


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

    seed_regressions: list[dict] = []
    if parent_key_clean:
        import logging as _logging
        _log = _logging.getLogger(__name__)
        try:
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as session:
                seed_regressions = await bug_analysis_repository.find_seed_regression_tests(
                    session,
                    ticket_key=request.ticket_key,
                    parent_key=parent_key_clean,
                    limit=5,
                )
            seed_count = sum(len(s.get("regression_tests") or []) for s in seed_regressions)
            _log.info(
                "seed_regressions: ticket=%s parent=%s sources=%d total_tests=%d",
                request.ticket_key, parent_key_clean, len(seed_regressions), seed_count,
            )
        except Exception:
            _log.exception("find_seed_regression_tests failed; continuing without seeds")

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
            child_info=request.child_info,
            linked_info=request.linked_info,
            slack_messages=slack_messages_for_prompt,
            seed_regressions=seed_regressions or None,
            bounce_history=request.bounce_history,
        )

        response = {
            "ticket_key": request.ticket_key,
            "happy_path": test_plan.happy_path,
            "edge_cases": test_plan.edge_cases,
            "regression_checklist": test_plan.regression_checklist,
            "integration_tests": test_plan.integration_tests or [],
            "grounding_warnings": _normalize_grounding_warnings(test_plan),
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

    aggregated_flags = {
        "had_pr_diff": False,
        "had_figma": False,
        "had_parent": False,
        "linked_ticket_count": 0,
        "pr_count": 0,
        "comment_count": 0,
    }
    for t in request.tickets:
        flags = _derive_context_flags(t)
        aggregated_flags["had_pr_diff"] = aggregated_flags["had_pr_diff"] or flags["had_pr_diff"]
        aggregated_flags["had_figma"] = aggregated_flags["had_figma"] or flags["had_figma"]
        aggregated_flags["had_parent"] = aggregated_flags["had_parent"] or flags["had_parent"]
        aggregated_flags["linked_ticket_count"] += flags["linked_ticket_count"]
        aggregated_flags["pr_count"] += flags["pr_count"]
        aggregated_flags["comment_count"] += flags["comment_count"]

    run_ctx = await run_tracker.start_run(
        run_type=RunType.test_plan,
        ticket_keys=[t.ticket_key for t in request.tickets],
        model=os.environ.get("LLM_MODEL", "unknown"),
        llm_provider=os.environ.get("LLM_PROVIDER", "unknown"),
        **aggregated_flags,
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
                "child_info": t.child_info,
                "linked_info": t.linked_info,
                "acceptance_criteria": extract_acceptance_criteria(t.description),
            }
            for t in request.tickets
        ]

        llm = get_llm_client()
        test_plan = await llm.generate_multi_ticket_test_plan(
            tickets=tickets_data,
            images=all_images,
        )

        ac_coverage = _compute_ac_coverage(test_plan, tickets_data)
        valid_ac_ids = {
            f"{t['ticket_key']}-AC{i}"
            for t in tickets_data
            for i in range(1, len(t.get("acceptance_criteria") or []) + 1)
        }
        grounding_warnings = _normalize_grounding_warnings(test_plan, valid_ac_ids)

        response = {
            "ticket_keys": [t.ticket_key for t in request.tickets],
            "happy_path": test_plan.happy_path,
            "edge_cases": test_plan.edge_cases,
            "regression_checklist": test_plan.regression_checklist,
            "integration_tests": test_plan.integration_tests or [],
            "ac_coverage": ac_coverage,
            "superseded_acs": ac_coverage.get("superseded_acs", []),
            "grounding_warnings": grounding_warnings,
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
