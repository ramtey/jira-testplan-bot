"""
QA workflow routes — single-click status transitions plus reassignment.

Mounted at /issue in main.py via APIRouter, mirroring the bug_lens_routes and
runs_routes pattern. Hardcoded to the SK project for now; generalize via a
per-project config once a second project needs it.
"""

import asyncio
import logging
import re

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .db.session import get_sessionmaker
from .jira_client import (
    ImageAttachment,
    JiraAuthError,
    JiraClient,
    JiraConnectionError,
    JiraNotFoundError,
    is_blocked_bot_display_name,
)
from .models import WorkflowActionRequest
from .repositories import walkthrough_repository
from . import uat_readiness

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/issue", tags=["workflow"])


SK_WORKFLOW_ACTIONS: dict[str, str] = {
    "pull-to-testing": "In Testing",
    "pass-to-uat": "Ready for UAT",
    "fail-to-todo": "To Do",
    "fail-to-in-progress": "In Progress",
}

# Bounce-back actions that return a ticket to development with a required
# reason + fail comment. Both behave identically aside from the target column.
_FAIL_ACTIONS = {"fail-to-todo", "fail-to-in-progress"}


_ATTACHMENT_CONTENT_URL_RE = re.compile(r"/attachment/content/(\d+)")


def _attachment_id_from_content_url(url: str) -> str | None:
    """Pull the numeric attachment id out of a Jira `content` URL.

    Walkthrough screenshots persist their download URL (shape:
    `.../rest/api/3/attachment/content/<id>`). To render legacy
    entries inline we need to re-resolve their media-services UUID,
    which requires that numeric id. Returns None if the URL doesn't
    match the expected shape (foreign host, empty, malformed).
    """
    if not url:
        return None
    match = _ATTACHMENT_CONTENT_URL_RE.search(url)
    return match.group(1) if match else None


async def _resolve_media_ids_for_urls(
    jira: "JiraClient", attachment_ids: list[str | None]
) -> dict[str, str | None]:
    """Resolve media UUIDs for a set of numeric attachment ids in parallel.

    Returns a dict keyed by attachment id. Unknown / failed lookups
    map to None so the caller can fall back to a plain-text callout.
    """
    unique_ids = [aid for aid in {aid for aid in attachment_ids if aid}]
    if not unique_ids:
        return {}
    resolved = await asyncio.gather(*[jira.resolve_media_id(aid) for aid in unique_ids])
    return dict(zip(unique_ids, resolved))


_ALLOWED_IMAGE_MIME = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
    "application/pdf",
}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB per file; Jira allows more but this is a sane UI cap.


@router.post("/{issue_key}/workflow/{action}")
async def run_workflow_action(
    issue_key: str,
    action: str,
    payload: str | None = Form(default=None),
    images: list[UploadFile] | None = File(default=None),
):
    """Execute a single-click QA workflow action: transition + reassign.

    The endpoint takes `multipart/form-data`: a JSON-encoded
    `WorkflowActionRequest` in the `payload` field, plus zero or more
    `images[]` files. When images are present they are uploaded to the
    issue as Jira attachments *before* the workflow transition runs, so
    a failed upload aborts cleanly without moving the ticket.
    """
    if not issue_key.upper().startswith("SK-"):
        raise HTTPException(
            status_code=400,
            detail="Workflow actions are only enabled for the SK project right now.",
        )
    if action not in SK_WORKFLOW_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    parsed_payload: WorkflowActionRequest | None = None
    if payload:
        try:
            parsed_payload = WorkflowActionRequest.model_validate_json(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid payload JSON: {exc}")

    # Pass-to-UAT readiness gate — enforce the "high-complexity ticket needs a
    # walkthrough" rule server-side so bypassing the frontend can't sneak an
    # unwalked-through ticket into UAT. Fires *before* any Jira calls: a 409
    # here leaves the ticket exactly where it was. The client sets the override
    # flag once the user has consciously acknowledged the missing walkthrough
    # (or when it sees material the server can't — e.g. PR-attached demo video).
    if action == "pass-to-uat":
        override = bool(
            parsed_payload and parsed_payload.override_missing_walkthrough
        )
        form_looms = bool(
            parsed_payload
            and parsed_payload.loom_urls
            and any(u and u.strip() for u in parsed_payload.loom_urls)
        )
        # Form-uploaded images are counted below via `images`; the raw
        # UploadFile list is still open at this point, so we test filenames
        # rather than reading bytes just to gate the request.
        form_images = bool(
            images and any(u and u.filename for u in images)
        )
        if not override and not form_looms and not form_images:
            async with get_sessionmaker()() as session:
                readiness = await uat_readiness.fetch_readiness(
                    session, ticket_key=issue_key
                )
            if readiness.get("needs_walkthrough"):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_code": "walkthrough_required",
                        "uat_complexity": readiness.get("uat_complexity"),
                        "message": (
                            "This ticket is high-complexity for UAT and has "
                            "no walkthrough attached. Add a Loom, screenshot, "
                            "or notes on the walkthrough card, or resubmit "
                            "with override_missing_walkthrough=true."
                        ),
                    },
                )

    image_files: list[tuple[str, bytes, str]] = []
    if images:
        for upload in images:
            if upload is None or not upload.filename:
                continue
            mime = (upload.content_type or "").lower()
            if mime not in _ALLOWED_IMAGE_MIME:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported attachment type: {mime or 'unknown'}. "
                           f"Allowed: PNG, JPEG, GIF, WEBP, PDF.",
                )
            content = await upload.read()
            if len(content) > _MAX_IMAGE_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=f"{upload.filename} is larger than 10 MB.",
                )
            image_files.append((upload.filename, content, mime))

    target_status = SK_WORKFLOW_ACTIONS[action]
    jira = JiraClient()

    # Upload attachments first so that a Jira-side failure aborts before
    # the ticket moves. Skipped for pull-to-testing — that action has no
    # comment flow, so any images would be orphaned attachments. We
    # resolve each upload's media-services UUID (the 303-redirect trick
    # on the content URL — see JiraClient.resolve_media_id) so the ADF
    # builder can embed the image inline via `mediaSingle` rather than
    # just linking a filename. UUID lookup failures fall back to plain
    # `📷 <filename>` callouts so the comment still posts.
    image_attachments: list[ImageAttachment] = []
    if image_files and (action == "pass-to-uat" or action in _FAIL_ACTIONS):
        try:
            uploaded = await jira.upload_attachments(issue_key, image_files)
            image_attachments = await jira.enrich_attachments_with_media_ids(uploaded)
        except JiraNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except JiraAuthError as e:
            raise HTTPException(status_code=e.status_code, detail=str(e))
        except JiraConnectionError as e:
            raise HTTPException(status_code=502, detail=str(e))

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

        logger.info(
            "Workflow %s on %s: resolved assignee via %s -> %s",
            action,
            issue_key,
            resolved_via,
            assigned_label,
        )

        # Capture parent's status BEFORE the transition so the subtask
        # cascade can match only siblings that share the parent's
        # pre-transition state (e.g., a parent in "Ready to Test" should
        # only pull subtasks that are also in "Ready to Test").
        parent_pre_status: str | None = None
        if parsed_payload is not None and parsed_payload.cascade_to_subtasks:
            try:
                parent_pre_status = await jira.get_issue_status(issue_key)
            except (JiraNotFoundError, JiraAuthError, JiraConnectionError) as exc:
                logger.warning(
                    "Could not read pre-transition status for %s: %s",
                    issue_key,
                    exc,
                )

        await jira.transition_issue(issue_key, transition["id"])
        await jira.assign_issue(issue_key, target_account_id)

        comment_posted = False
        if action == "pass-to-uat":
            # Fold the ticket's saved walkthrough (Loom / screenshot / notes)
            # into the UAT hand-off comment so the "how to test this" guidance
            # travels with the transition even when the form was left empty.
            looms = (
                list(parsed_payload.loom_urls)
                if parsed_payload and parsed_payload.loom_urls
                else []
            )
            summary = (parsed_payload.summary if parsed_payload else None) or ""
            environments = parsed_payload.environments if parsed_payload else None
            mentions = parsed_payload.mention_account_ids if parsed_payload else None
            try:
                async with get_sessionmaker()() as session:
                    walkthrough = await walkthrough_repository.get_walkthrough(
                        session, ticket_key=issue_key
                    )
            except Exception:
                walkthrough = None
            if walkthrough:
                if walkthrough.loom_url and walkthrough.loom_url not in looms:
                    looms.insert(0, walkthrough.loom_url)
                if walkthrough.notes:
                    summary = (
                        f"{summary}\n\n{walkthrough.notes}".strip()
                        if summary
                        else walkthrough.notes
                    )
                # The walkthrough's screenshots are already Jira attachments
                # (uploaded when the planner saved them); fold each into
                # image_attachments so post_qa_pass_comment renders them
                # inline the same way screenshots uploaded from the UAT
                # modal do. Legacy walkthrough entries were persisted
                # before media_id was captured — re-resolve their UUIDs
                # from the content URL so old walkthroughs still render
                # inline instead of falling back to text.
                walkthrough_shots = walkthrough_repository.decode_screenshots(
                    walkthrough
                )
                seen_urls = {img.url for img in image_attachments}
                pending: list[tuple[str, str, str | None]] = []
                for shot in walkthrough_shots:
                    if shot["url"] in seen_urls:
                        continue
                    seen_urls.add(shot["url"])
                    pending.append(
                        (shot["filename"], shot["url"], shot.get("media_id"))
                    )
                to_resolve = [
                    _attachment_id_from_content_url(url)
                    for _, url, media_id in pending
                    if not media_id
                ]
                if to_resolve:
                    resolved = await _resolve_media_ids_for_urls(jira, to_resolve)
                else:
                    resolved = {}
                to_prepend: list[ImageAttachment] = []
                for filename, url, media_id in pending:
                    if not media_id:
                        att_id = _attachment_id_from_content_url(url)
                        media_id = resolved.get(att_id) if att_id else None
                    to_prepend.append(
                        ImageAttachment(
                            filename=filename, url=url, media_id=media_id
                        )
                    )
                image_attachments = to_prepend + image_attachments
            try:
                result = await jira.post_qa_pass_comment(
                    issue_key,
                    looms or None,
                    summary or None,
                    environments,
                    mentions,
                    image_attachments or None,
                )
                comment_posted = result is not None
            except Exception as exc:
                # Transition + reassign already succeeded — surface the
                # comment failure but don't roll back the workflow move.
                # Catches httpx.HTTPStatusError too (e.g., Jira ADF
                # validation 400s) so the response carries CORS headers
                # instead of bubbling as a bare 500.
                logger.warning(
                    "pass-to-uat comment failed on %s: %s", issue_key, exc
                )
        elif action in _FAIL_ACTIONS and parsed_payload is not None:
            try:
                result = await jira.post_qa_fail_comment(
                    issue_key,
                    parsed_payload.reason,
                    parsed_payload.loom_urls,
                    image_attachments or None,
                    parsed_payload.mention_account_ids,
                )
                comment_posted = result is not None
            except Exception as exc:
                logger.warning(
                    "%s comment failed on %s: %s", action, issue_key, exc
                )

        parent_transitioned = False
        parent_key: str | None = None
        if action == "pass-to-uat":
            parent_transitioned, parent_key = await _maybe_transition_parent_to_uat(
                jira, issue_key, target_status
            )

        cascaded_subtasks: list[str] = []
        if parsed_payload is not None and parsed_payload.cascade_to_subtasks:
            cascaded_subtasks = await _cascade_transition_to_subtasks(
                jira, issue_key, target_status, parent_pre_status
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
            logger.info(
                "Skipping parent auto-transition: parent %s has no '%s' transition available",
                parent_key,
                target_status,
            )
            return False, parent_key

        await jira.transition_issue(parent_key, transition["id"])
        logger.info(
            "Auto-transitioned parent %s to %s after last subtask %s passed to UAT",
            parent_key,
            target_status,
            subtask_key,
        )
        return True, parent_key
    except (JiraNotFoundError, JiraAuthError, JiraConnectionError) as exc:
        logger.warning(
            "Parent auto-transition failed for %s: %s", subtask_key, exc
        )
        return False, None


async def _cascade_transition_to_subtasks(
    jira: JiraClient,
    parent_key: str,
    target_status: str,
    parent_pre_status: str | None = None,
) -> list[str]:
    """Transition direct subtasks of `parent_key` to `target_status`.

    Only subtasks whose current status matches `parent_pre_status` (the
    parent's status *before* it was transitioned) are moved — so a parent
    advancing from "Ready to Test" only pulls subtasks that were also in
    "Ready to Test", leaving siblings in unrelated states alone. When
    `parent_pre_status` is unknown (None), the legacy behavior of moving
    every eligible subtask is preserved. Subtasks already at the target,
    or whose workflow has no matching transition, are skipped silently.
    Returns the list of subtask keys that were actually moved.
    """
    moved: list[str] = []
    target_lower = target_status.strip().lower()
    parent_lower = (parent_pre_status or "").strip().lower()

    try:
        subtasks = await jira.get_subtasks_of(parent_key)
    except (JiraNotFoundError, JiraAuthError, JiraConnectionError) as exc:
        logger.warning(
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
        if parent_lower and status_name != parent_lower:
            logger.info(
                "Cascade skip: subtask %s status '%s' does not match parent's pre-transition status '%s'",
                sub_key,
                status_name,
                parent_lower,
            )
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
                logger.info(
                    "Cascade skip: subtask %s has no transition to '%s'",
                    sub_key,
                    target_status,
                )
                continue
            await jira.transition_issue(sub_key, transition["id"])
            moved.append(sub_key)
        except (JiraNotFoundError, JiraAuthError, JiraConnectionError) as exc:
            logger.warning(
                "Cascade transition failed for %s: %s", sub_key, exc
            )

    return moved
