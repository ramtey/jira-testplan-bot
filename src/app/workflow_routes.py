"""
QA workflow routes — single-click status transitions plus reassignment.

Mounted at /issue in main.py via APIRouter, mirroring the bug_lens_routes and
runs_routes pattern. Hardcoded to the SK project for now; generalize via a
per-project config once a second project needs it.
"""

import asyncio
import logging
import mimetypes
import posixpath
import re
import time
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from .config import settings
from .db.session import get_sessionmaker
from .github_client import GitHubClient
from .jira_client import (
    ImageAttachment,
    JiraAuthError,
    JiraClient,
    JiraConnectionError,
    JiraNotFoundError,
    is_blocked_bot_display_name,
)
from .models import LOOM_URL_RE, WorkflowActionRequest
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


# Loom URL shape lives in models.LOOM_URL_RE so the WorkflowActionRequest
# validator and the PR-description scraper below can't drift. We .search()
# here (URLs are embedded in prose / markdown link targets) rather than
# .fullmatch() — trailing punctuation like "video: …loom.com/share/abc."
# is naturally excluded by the character class.


# GitHub-hosted image URLs that show up in PR descriptions. Three shapes:
#   1) github.com/user-attachments/assets/<uuid>            (newest — private repos)
#   2) (private-)user-images.githubusercontent.com/…/<name>.<ext>   (older)
#   3) camo.githubusercontent.com/<hash>/<b64-target>       (proxied external)
# Only these hosts are matched so we don't accidentally treat linked
# non-image assets (favicons, tracking pixels, arbitrary CDN URLs) as
# screenshots. Keep in sync with `PR_IMAGE_ALLOWED_HOSTS` below — the
# validator on the request payload and the proxy endpoint both reuse
# this whitelist to gate what the client can ask us to fetch.
PR_IMAGE_ALLOWED_HOSTS = frozenset({
    "github.com",
    "user-images.githubusercontent.com",
    "private-user-images.githubusercontent.com",
    "camo.githubusercontent.com",
})
_PR_IMAGE_URL_RE = re.compile(
    r"https?://(?:"
    r"github\.com/user-attachments/assets/[A-Za-z0-9\-]+"
    r"|(?:private-)?user-images\.githubusercontent\.com/[A-Za-z0-9/_.\-%?=&]+?"
    r"\.(?:png|jpe?g|gif|webp)"
    r"|camo\.githubusercontent\.com/[A-Za-z0-9/_.\-%]+"
    r")",
    re.IGNORECASE,
)


async def _harvest_loom_urls_from_merged_prs(
    jira: "JiraClient", issue_key: str
) -> tuple[list[str], list[str], str]:
    """Pull Loom share URLs and GitHub-hosted image URLs out of the
    *description* of merged PRs linked to an issue. Description-only on
    purpose: PR review comments are dev-facing chatter and inflate noise;
    the author-written body is the closest thing to a curated demo pointer.

    Merge state comes from Jira's dev-status API (not GitHub's `merged`
    field) so a transient GitHub 403/404 doesn't collapse into a
    misleading "nothing is merged yet." The GitHub call is still needed
    to read the PR body for Loom URLs, but its failure surfaces as
    `github_unreachable`, distinct from "no merged PRs exist."

    Returns (loom_urls, image_urls, status). `status` reports why the
    combined list is empty when it is:
      - "found"              — at least one Loom OR image harvested
      - "no_token"           — GITHUB_TOKEN not configured (server-side)
      - "no_prs"             — no PRs linked to this issue at all
      - "no_merged_prs"      — PRs exist but none marked MERGED in Jira
      - "no_looms"           — merged PRs exist but no media in any body
                               (name preserved for frontend backwards-compat
                               — the panel copy already reads "no Loom link";
                               update in tandem if the copy changes)
      - "github_unreachable" — merged PRs exist but every GitHub fetch failed
      - "error"              — a Jira call raised; opt-in enrichment,
                               never blocks the UAT hand-off

    Both lists are always [] when status != "found".
    """
    if not settings.github_token:
        return [], [], "no_token"
    try:
        issue_id = await jira._get_issue_internal_id(issue_key)
    except Exception:
        logger.exception("PR-Loom harvest for %s: failed to resolve Jira issue id", issue_key)
        return [], [], "error"
    if not issue_id:
        return [], [], "no_prs"
    try:
        pr_rows = await jira._list_dev_status_pr_summaries(issue_id)
    except Exception:
        logger.exception(
            "PR-Loom harvest for %s: dev-status PR lookup raised (issue_id=%s)",
            issue_key,
            issue_id,
        )
        return [], [], "error"
    github_rows = [
        row for row in pr_rows if row.get("url") and "github.com" in row["url"]
    ]
    if not github_rows:
        return [], [], "no_prs"
    merged_urls = [
        row["url"]
        for row in github_rows
        if (row.get("status") or "").strip().upper() == "MERGED"
    ]
    if not merged_urls:
        logger.info(
            "PR-Loom harvest for %s: %d linked PR(s), none marked MERGED (states=%s)",
            issue_key,
            len(github_rows),
            [row.get("status") for row in github_rows],
        )
        return [], [], "no_merged_prs"
    github_client = GitHubClient()
    details_list = await asyncio.gather(
        *[
            github_client.fetch_pr_details(
                url, include_patch=False, include_comments=False
            )
            for url in merged_urls
        ],
        return_exceptions=True,
    )
    fetched_any = False
    looms: list[str] = []
    images: list[str] = []
    seen_looms: set[str] = set()
    seen_images: set[str] = set()
    for details in details_list:
        if isinstance(details, Exception) or details is None:
            continue
        fetched_any = True
        body = details.description or ""
        for match in LOOM_URL_RE.finditer(body):
            url = match.group(0).rstrip(".,);:]")
            if url not in seen_looms:
                seen_looms.add(url)
                looms.append(url)
        for match in _PR_IMAGE_URL_RE.finditer(body):
            url = match.group(0).rstrip(".,);:]")
            if url not in seen_images:
                seen_images.add(url)
                images.append(url)
    if looms or images:
        return looms, images, "found"
    if not fetched_any:
        logger.warning(
            "PR-Loom harvest for %s: %d merged PR(s) but every GitHub fetch failed",
            issue_key,
            len(merged_urls),
        )
        return [], [], "github_unreachable"
    return [], [], "no_looms"


_ALLOWED_IMAGE_MIME = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
    "application/pdf",
}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB per file; Jira allows more but this is a sane UI cap.


def _parse_workflow_payload(payload: str | None) -> WorkflowActionRequest | None:
    if not payload:
        return None
    try:
        return WorkflowActionRequest.model_validate_json(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid payload JSON: {exc}")


async def _enforce_pass_to_uat_walkthrough_gate(
    issue_key: str,
    parsed_payload: WorkflowActionRequest | None,
    images: list[UploadFile] | None,
) -> None:
    """Server-side gate for the "high-complexity ticket needs a walkthrough" rule.

    Fires *before* any Jira calls so a 409 here leaves the ticket exactly
    where it was. The frontend sets `override_missing_walkthrough` once the
    tester has consciously acknowledged the missing walkthrough (or when it
    sees material the server can't — e.g., PR-attached demo video).
    """
    override = bool(parsed_payload and parsed_payload.override_missing_walkthrough)
    form_looms = bool(
        parsed_payload
        and (
            (
                parsed_payload.loom_urls
                and any(u and u.strip() for u in parsed_payload.loom_urls)
            )
            or (
                parsed_payload.pr_loom_urls
                and any(u and u.strip() for u in parsed_payload.pr_loom_urls)
            )
        )
    )
    # Form-uploaded images are counted below via `images`; the raw UploadFile
    # list is still open at this point, so we test filenames rather than
    # reading bytes just to gate the request.
    form_images = bool(images and any(u and u.filename for u in images))
    # PR-scraped images the tester ticked count the same as a hand-uploaded
    # screenshot — they'll be downloaded and attached below.
    pr_images = bool(
        parsed_payload
        and parsed_payload.pr_image_urls
        and any(u and u.strip() for u in parsed_payload.pr_image_urls)
    )
    if override or form_looms or form_images or pr_images:
        return

    async with get_sessionmaker()() as session:
        readiness = await uat_readiness.fetch_readiness(session, ticket_key=issue_key)
    if not readiness.get("needs_walkthrough"):
        return

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


async def _validate_and_read_images(
    images: list[UploadFile] | None,
) -> list[tuple[str, bytes, str]]:
    image_files: list[tuple[str, bytes, str]] = []
    if not images:
        return image_files
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
    return image_files


async def _upload_and_enrich_attachments(
    jira: JiraClient,
    issue_key: str,
    image_files: list[tuple[str, bytes, str]],
) -> list[ImageAttachment]:
    """Upload attachments and resolve each one's media-services UUID.

    Runs *before* the workflow transition so a Jira-side failure aborts
    without moving the ticket. Media UUIDs let the ADF builder embed
    each image inline via `mediaSingle`; unresolved uploads fall back to
    a `📷 <filename>` text callout.
    """
    try:
        uploaded = await jira.upload_attachments(issue_key, image_files)
        return await jira.enrich_attachments_with_media_ids(uploaded)
    except JiraNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except JiraAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except JiraConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))


async def _lookup_target_transition(
    jira: JiraClient, issue_key: str, target_status: str
) -> dict:
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
    return transition


async def _resolve_assignee_for_workflow(
    jira: JiraClient,
    issue_key: str,
    action: str,
    parsed_payload: WorkflowActionRequest | None,
    my_account_id: str | None,
) -> tuple[str | None, str, str]:
    """Return (target_account_id, assigned_label, resolved_via)."""
    override_assignee = bool(parsed_payload and parsed_payload.assignee_override_set)
    if action == "pull-to-testing":
        # pull-to-testing always parks the ticket on the bot's own account;
        # a manual override on this action makes no sense (there's no form
        # in the UI that exposes it), so we don't honor it here.
        return my_account_id, "you", "self"

    if override_assignee:
        # Tester picked a specific person (or "unassign") in the form — skip
        # the auto-pick chain entirely and use the choice verbatim. Display
        # name is passed through so the response label matches the pill they
        # clicked without an extra Jira round-trip.
        target_account_id = parsed_payload.assignee_override_account_id
        if target_account_id:
            assigned_label = (
                parsed_payload.assignee_override_display_name or "selected assignee"
            )
        else:
            assigned_label = "unassigned"
        return target_account_id, assigned_label, "manual-override"

    # Exclude the bot's own account from both lookups: pull-to-testing always
    # parks the ticket on the bot, so the bot showing up as a prior `from` (or
    # as a loose name match in PR-contributor search) is noise, not a real
    # developer to hand the ticket back to.
    target_account_id, prior_name = await jira.get_prior_assignee_account_id(
        issue_key, exclude_account_id=my_account_id
    )
    if target_account_id:
        return target_account_id, prior_name or "prior assignee", "prior-assignee"

    target_account_id, contributor_name = await jira.get_top_pr_contributor_account_id(
        issue_key, exclude_account_id=my_account_id
    )
    if target_account_id:
        return target_account_id, contributor_name or "top contributor", "pr-contributor"

    return None, "unassigned", "unassigned"


def _apply_bot_safety_net(
    target_account_id: str | None,
    assigned_label: str,
    resolved_via: str,
    my_account_id: str | None,
) -> tuple[str | None, str, str]:
    """If auto-pick OR manual override landed on a bot, unassign instead.

    Covers both a stale prior-assignee that's the bot and a tester picking
    the bot by mistake — either way, parking the ticket back on the bot is
    never what we want on pass/fail.
    """
    if (
        target_account_id == my_account_id
        or is_blocked_bot_display_name(assigned_label)
    ):
        return None, "unassigned", f"{resolved_via}+unassigned-safety-net"
    return target_account_id, assigned_label, resolved_via


async def _fold_walkthrough_into_pass_data(
    jira: JiraClient,
    issue_key: str,
    parsed_payload: WorkflowActionRequest | None,
    image_attachments: list[ImageAttachment],
) -> tuple[list[str], list[str], str, list[str] | None, list[str] | None, list[ImageAttachment]]:
    """Merge the saved walkthrough into the pass-to-UAT comment inputs.

    Returns (looms, pr_looms, summary, environments, mentions, image_attachments)
    with the walkthrough's Loom prepended to `looms`, its notes appended to
    `summary`, and its screenshots prepended to `image_attachments`. Legacy
    walkthrough screenshots that were persisted before `media_id` was
    captured get their UUID re-resolved from the content URL so old entries
    still render inline.
    """
    looms = (
        list(parsed_payload.loom_urls)
        if parsed_payload and parsed_payload.loom_urls
        else []
    )
    pr_looms = (
        list(parsed_payload.pr_loom_urls)
        if parsed_payload and parsed_payload.pr_loom_urls
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

    if not walkthrough:
        return looms, pr_looms, summary, environments, mentions, image_attachments

    if walkthrough.loom_url and walkthrough.loom_url not in looms:
        looms.insert(0, walkthrough.loom_url)
    if walkthrough.notes:
        summary = (
            f"{summary}\n\n{walkthrough.notes}".strip()
            if summary
            else walkthrough.notes
        )

    walkthrough_shots = walkthrough_repository.decode_screenshots(walkthrough)
    seen_urls = {img.url for img in image_attachments}
    pending: list[tuple[str, str, str | None]] = []
    for shot in walkthrough_shots:
        if shot["url"] in seen_urls:
            continue
        seen_urls.add(shot["url"])
        pending.append((shot["filename"], shot["url"], shot.get("media_id")))

    to_resolve = [
        _attachment_id_from_content_url(url)
        for _, url, media_id in pending
        if not media_id
    ]
    resolved = (
        await _resolve_media_ids_for_urls(jira, to_resolve) if to_resolve else {}
    )

    to_prepend: list[ImageAttachment] = []
    for filename, url, media_id in pending:
        if not media_id:
            att_id = _attachment_id_from_content_url(url)
            media_id = resolved.get(att_id) if att_id else None
        to_prepend.append(
            ImageAttachment(filename=filename, url=url, media_id=media_id)
        )
    return looms, pr_looms, summary, environments, mentions, to_prepend + image_attachments


async def _post_workflow_comment(
    jira: JiraClient,
    action: str,
    issue_key: str,
    parsed_payload: WorkflowActionRequest | None,
    image_attachments: list[ImageAttachment],
) -> bool:
    """Post the pass or fail comment after the transition has already run.

    Exceptions are swallowed and logged: the transition + reassign already
    succeeded, so surfacing a comment failure as a 500 would misrepresent
    the state of the ticket. Catches broad `Exception` on purpose to cover
    httpx errors (e.g., Jira ADF validation 400s) and keep CORS headers on
    the response.
    """
    if action == "pass-to-uat":
        looms, pr_looms, summary, environments, mentions, image_attachments = (
            await _fold_walkthrough_into_pass_data(
                jira, issue_key, parsed_payload, image_attachments
            )
        )
        try:
            result = await jira.post_qa_pass_comment(
                issue_key,
                looms or None,
                summary or None,
                environments,
                mentions,
                image_attachments or None,
                pr_looms or None,
            )
            return result is not None
        except Exception as exc:
            logger.warning("pass-to-uat comment failed on %s: %s", issue_key, exc)
            return False

    if action in _FAIL_ACTIONS and parsed_payload is not None:
        try:
            result = await jira.post_qa_fail_comment(
                issue_key,
                parsed_payload.reason,
                parsed_payload.loom_urls,
                image_attachments or None,
                parsed_payload.mention_account_ids,
            )
            return result is not None
        except Exception as exc:
            logger.warning("%s comment failed on %s: %s", action, issue_key, exc)
            return False

    return False


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

    parsed_payload = _parse_workflow_payload(payload)

    if action == "pass-to-uat":
        await _enforce_pass_to_uat_walkthrough_gate(issue_key, parsed_payload, images)

    image_files = await _validate_and_read_images(images)

    # PR-scraped screenshots the tester ticked ride the same upload path as
    # hand-attached files: download bytes via the GitHub token, then feed
    # them into the same enrich+upload step so they render inline as media
    # nodes in the pass comment. Only pass-to-uat supports this — fail-back
    # forms don't surface the PR panel client-side.
    if action == "pass-to-uat" and parsed_payload and parsed_payload.pr_image_urls:
        pr_image_files = await _download_pr_images_as_uploads(
            parsed_payload.pr_image_urls
        )
        image_files.extend(pr_image_files)

    target_status = SK_WORKFLOW_ACTIONS[action]
    jira = JiraClient()

    # Timing: the endpoint fans out 8-12 Jira REST calls per request, and
    # the tester perceives anything past ~10s as "stuck." Log the wall-clock
    # of each phase so a slow ticket can be traced to which Jira call is
    # actually dragging (upload, transition lookup, comment post, etc.).
    t_start = time.perf_counter()

    upload_needed = bool(image_files) and (
        action == "pass-to-uat" or action in _FAIL_ACTIONS
    )

    # Phase 1 — read-only prep + attachment upload, in parallel. All four
    # branches are independent of one another; the attachment upload writes
    # to Jira (as attachments, not the ticket status), so a failure here
    # still aborts before the primary transition runs, matching the pre-
    # refactor "upload before transition" invariant.
    #
    # my_account_id has a process-wide cache, so awaiting it before the
    # gather keeps _resolve_assignee_for_workflow's signature unchanged
    # without adding a round-trip.
    try:
        my_account_id = await jira.get_my_account_id()

        async def _do_upload() -> list[ImageAttachment]:
            if not upload_needed:
                return []
            return await _upload_and_enrich_attachments(
                jira, issue_key, image_files
            )

        async def _do_parent_pre_status() -> str | None:
            if parsed_payload is None or not parsed_payload.cascade_to_subtasks:
                return None
            try:
                return await jira.get_issue_status(issue_key)
            except (JiraNotFoundError, JiraAuthError, JiraConnectionError) as exc:
                logger.warning(
                    "Could not read pre-transition status for %s: %s",
                    issue_key,
                    exc,
                )
                return None

        (
            image_attachments,
            transition,
            assignee_result,
            parent_pre_status,
        ) = await asyncio.gather(
            _do_upload(),
            _lookup_target_transition(jira, issue_key, target_status),
            _resolve_assignee_for_workflow(
                jira, issue_key, action, parsed_payload, my_account_id
            ),
            _do_parent_pre_status(),
        )
        target_account_id, assigned_label, resolved_via = assignee_result
        if action != "pull-to-testing":
            target_account_id, assigned_label, resolved_via = _apply_bot_safety_net(
                target_account_id, assigned_label, resolved_via, my_account_id
            )
        t_prep = time.perf_counter() - t_start

        logger.info(
            "Workflow %s on %s: resolved assignee via %s -> %s",
            action,
            issue_key,
            resolved_via,
            assigned_label,
        )

        # Phase 2 — transition + assign, in parallel. Both are single writes
        # against the same ticket but hit independent endpoints
        # (`/transitions` vs `/assignee`) so Jira handles them concurrently.
        # If either fails the other still completes; that matches the pre-
        # refactor sequential behavior (transition-then-assign left the same
        # partial-write windows on either half's failure).
        await asyncio.gather(
            jira.transition_issue(issue_key, transition["id"]),
            jira.assign_issue(issue_key, target_account_id),
        )
        t_primary = time.perf_counter() - t_start

        # Phase 3 — follow-ups, all parallel. Comment posting swallows its
        # own errors; parent/cascade helpers catch their Jira exceptions and
        # log-and-return. None of them can propagate to the outer handler,
        # so gather here can't fail after the transition already succeeded.
        async def _do_comment() -> bool:
            return await _post_workflow_comment(
                jira, action, issue_key, parsed_payload, image_attachments
            )

        async def _do_parent_transition() -> tuple[bool, str | None]:
            if action != "pass-to-uat":
                return False, None
            return await _maybe_transition_parent_to_uat(
                jira, issue_key, target_status
            )

        async def _do_cascade() -> list[str]:
            if parsed_payload is None or not parsed_payload.cascade_to_subtasks:
                return []
            return await _cascade_transition_to_subtasks(
                jira, issue_key, target_status, parent_pre_status
            )

        (
            comment_posted,
            (parent_transitioned, parent_key),
            cascaded_subtasks,
        ) = await asyncio.gather(
            _do_comment(),
            _do_parent_transition(),
            _do_cascade(),
        )
        t_total = time.perf_counter() - t_start

        logger.info(
            "Workflow %s on %s finished in %.2fs "
            "(prep=%.2fs primary=%.2fs follow-ups=%.2fs)",
            action,
            issue_key,
            t_total,
            t_prep,
            t_primary - t_prep,
            t_total - t_primary,
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


@router.get("/{issue_key}/pr-looms")
async def get_pr_looms(issue_key: str) -> dict:
    """Preview endpoint for the Pass-to-UAT modal.

    Returns Loom share URLs and GitHub-hosted image URLs found in the
    description of merged PRs linked to this issue, plus a `status` telling
    the frontend *why* both lists are empty when they are. Always 200 —
    this is opt-in enrichment, not a gate.

    Endpoint name is preserved for stability even though it now also
    surfaces images; the frontend reads `loom_urls` and `image_urls`
    off the same response. Image URLs are rendered via
    `/pr-image-proxy?url=…` so previews work for private-repo assets
    that the browser can't authenticate to directly.

    Status values: see `_harvest_loom_urls_from_merged_prs`. The endpoint
    adds `"skipped"` for tickets outside the SK-project gate so the modal
    can stay silent instead of showing a misleading "no media" line for
    a project the feature doesn't cover.
    """
    if not issue_key.upper().startswith("SK-"):
        return {"loom_urls": [], "image_urls": [], "status": "skipped"}
    jira = JiraClient()
    loom_urls, image_urls, status = await _harvest_loom_urls_from_merged_prs(
        jira, issue_key
    )
    return {
        "loom_urls": loom_urls,
        "image_urls": image_urls,
        "status": status,
    }


# Cap on how many bytes we'll pipe through the proxy in one shot. Set to
# the same 10 MB ceiling that gates hand-uploaded screenshots so the two
# entry points behave symmetrically — a browser preview and a submit
# accept the same size envelope.
_PR_IMAGE_PROXY_MAX_BYTES = _MAX_IMAGE_BYTES


def _guard_pr_image_url(url: str) -> str:
    """Validate a PR-image URL against the host whitelist.

    Returns the normalized URL, raises HTTPException on anything outside
    the whitelist. Shared by the proxy endpoint (preview time) and the
    pass-to-uat downloader (submit time) so a URL the validator on the
    payload rejects can't slip in via a differently-shaped query string.
    """
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="url must be http(s)")
    if parsed.hostname not in PR_IMAGE_ALLOWED_HOSTS:
        raise HTTPException(
            status_code=400,
            detail=f"host {parsed.hostname!r} is not an accepted PR image host",
        )
    return url


async def _fetch_pr_image_bytes(url: str) -> tuple[bytes, str]:
    """Pull raw bytes for a PR-hosted image URL through the GitHub token.

    Uses the same `Authorization: Bearer …` header the rest of the
    GitHub client uses so private-repo user-attachments assets resolve
    (they 302 to a signed CDN URL — httpx follows the redirect and the
    Bearer header rides along until the redirect target is off-host,
    at which point httpx drops it automatically). Returns (bytes,
    content_type). Raises HTTPException on any transport error so the
    caller can surface a specific status back to the client instead of
    a bare 500.
    """
    headers = {"User-Agent": "jira-testplan-bot"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"GitHub fetch failed: {exc}"
        )
    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail=(
                f"GitHub returned {response.status_code} for the "
                f"image URL — the repo may be private or the token "
                f"is missing 'repo' scope."
            ),
        )
    content = response.content
    if len(content) > _PR_IMAGE_PROXY_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Image exceeds the {_PR_IMAGE_PROXY_MAX_BYTES // (1024 * 1024)} MB "
                f"cap ({len(content)} bytes)."
            ),
        )
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if not content_type:
        # Fallback: guess from the URL path. GitHub asset URLs sometimes come
        # back with `application/octet-stream` too — treat those the same and
        # let the ADF builder decide whether the extension is renderable.
        guessed, _ = mimetypes.guess_type(urlparse(url).path)
        content_type = guessed or "application/octet-stream"
    return content, content_type


@router.get("/pr-image-proxy")
async def pr_image_proxy(url: str) -> Response:
    """Authenticated pass-through for a whitelisted GitHub image URL.

    The preview panel on the Pass-to-UAT form loads image thumbnails
    through this endpoint instead of hitting GitHub directly, so
    private-repo assets (which require the token) still render inline
    in the browser. Not mounted under `/issue/{key}` because the URL
    itself already identifies the resource — no ticket context needed.
    Returns 4xx via `_guard_pr_image_url` / `_fetch_pr_image_bytes` on
    anything the whitelist rejects, so this is not an open proxy.
    """
    _guard_pr_image_url(url)
    content, content_type = await _fetch_pr_image_bytes(url)
    # Cache aggressively — the GitHub asset id is immutable, and the
    # preview panel re-renders on every form open otherwise.
    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


def _filename_for_pr_image(url: str, content_type: str, index: int) -> str:
    """Pick a stable filename for a PR-scraped image attachment.

    Uses the last path segment when the URL ends in one (older
    `user-images.githubusercontent.com` links carry the original filename),
    otherwise falls back to `pr-screenshot-N.<ext>`. Kept deterministic
    per (issue, url, index) so re-runs don't spawn duplicate attachments
    with different names.
    """
    path = urlparse(url).path or ""
    tail = posixpath.basename(path)
    if tail and "." in tail:
        return tail
    ext = mimetypes.guess_extension(content_type or "") or ".png"
    return f"pr-screenshot-{index + 1}{ext}"


async def _download_pr_images_as_uploads(
    pr_image_urls: list[str] | None,
) -> list[tuple[str, bytes, str]]:
    """Materialize selected PR image URLs into upload tuples.

    Shape matches what `_upload_and_enrich_attachments` expects
    (`(filename, bytes, mime)`). One 4xx from the GitHub fetch aborts
    the whole pass-to-UAT submit — the tester ticked these deliberately,
    so silently dropping a broken URL would be a lie of omission, not
    graceful degradation.
    """
    if not pr_image_urls:
        return []
    out: list[tuple[str, bytes, str]] = []
    for i, raw in enumerate(pr_image_urls):
        url = (raw or "").strip()
        if not url:
            continue
        _guard_pr_image_url(url)
        content, content_type = await _fetch_pr_image_bytes(url)
        filename = _filename_for_pr_image(url, content_type, i)
        out.append((filename, content, content_type or "application/octet-stream"))
    return out


@router.get("/{issue_key}/pr-contributor")
async def get_pr_contributor(issue_key: str) -> dict:
    """Resolve the top PR contributor to a Jira user for the Pass-to-UAT picker.

    Runs the same lookup the server's assignee auto-pick chain uses when no
    prior developer is found, so the tester can *see* who the ticket would
    land on (and change it) before submitting. Always 200 — silent when no
    match: `{account_id: null, display_name: null}`.

    Excludes the token owner (bot / current user) so tickets where the tester
    is the only person in the history still surface the PR author instead of
    themselves.
    """
    jira = JiraClient()
    try:
        my_account_id = await jira.get_my_account_id()
    except Exception:
        my_account_id = None
    account_id, display_name = await jira.get_top_pr_contributor_account_id(
        issue_key, exclude_account_id=my_account_id
    )
    return {"account_id": account_id, "display_name": display_name}


@router.get("/users/search")
async def search_jira_users(q: str = "", limit: int = 5) -> dict:
    """Typeahead for the Pass-to-UAT "add someone else" picker.

    Short-circuits on empty/short queries so the frontend can wire this to
    every keystroke without hammering Jira on stray focus events. Silent on
    Jira errors — the picker just shows no results rather than surfacing a
    dropdown full of failure copy.
    """
    query = (q or "").strip()
    if len(query) < 2:
        return {"results": []}
    capped = max(1, min(limit, 10))
    try:
        results = await JiraClient().search_users(query, max_results=capped)
    except (JiraAuthError, JiraConnectionError) as exc:
        logger.warning("user search failed for %r: %s", query, exc)
        return {"results": []}
    return {"results": results}
