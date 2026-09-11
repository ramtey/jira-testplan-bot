"""Entry-point-agnostic test-plan generation.

Why this module exists
----------------------
The full pipeline — deliverable classifier, generation, the four
post-generation critics, AC coverage, run persistence — used to live
inline in the ``/generate-test-plan`` route handler, and the *caller*
was responsible for assembling the ticket payload. The web frontend did
that in ``useTestPlan.js``'s ``buildTicketPayload``; the MCP server and
the CLI each hand-rolled their own version and then called
``llm_client.generate_test_plan`` directly. That meant a plan generated
outside the browser silently skipped every critic, computed no AC
coverage, seeded no prior regressions, and persisted no run — the same
ticket produced a materially worse plan depending on which door you
came in through.

Everything now goes through here, so a plan is the same plan whatever
asked for it: the UI, the CLI, the MCP server, or a background watcher.

Two layers, deliberately separate:

* ``generate_single`` / ``generate_multi`` take an already-assembled
  payload. The HTTP routes call these, so the browser keeps working
  exactly as before (it already holds the ticket data it fetched).
* ``generate_for_ticket`` / ``generate_for_tickets`` take just Jira
  keys, fetch the context server-side, and delegate to the above.
  This is the door every non-browser caller should use.

Nothing in here raises ``HTTPException`` — that would make the module
unusable from the CLI, the MCP server, and any scheduled job. Domain
failures surface as ``NonTestableIssueError`` or as the underlying
``LLMError`` / ``Jira*Error``; mapping those onto status codes is the
route layer's job.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict

from ..config import NON_TESTABLE_ISSUE_TYPES, settings
from ..db.models.plan import PlanFormat
from ..db.models.run import RunType
from ..db.session import get_sessionmaker
from ..deliverable_classifier import (
    aggregate_deliverables_for_critique,
    format_deliverable_hint,
)
from ..description_analyzer import extract_acceptance_criteria
from ..jira_client import JiraClient
from ..llm_client import LLMError, get_llm_client
from ..models import GenerateTestPlanRequest, TicketInput
from ..repositories import bug_analysis_repository
from ..seam_extractor import build_seam_catalog, classify_multi_ticket_mode
from ..slack_client import resolve_slack_messages_in_text
from ..source_grounding import (
    filter_development_info,
    has_grounding_source,
    merge_provenance,
    no_source_result,
)
from . import run_tracker
from .test_plan_generator import (
    classify_deliverable,
    compute_ac_coverage,
    derive_context_flags,
    flatten_cases_for_persistence,
    normalize_grounding_warnings,
    quarantine_ungrounded_cases,
    run_code_grounding_critic,
    run_fix_scope_critic,
    run_grounding_critic,
    run_surface_mismatch_critic,
)

logger = logging.getLogger(__name__)

# Jira caps how many attachment images we ship to the model. Kept at the
# historical value so prompts (and cost) don't shift with this refactor.
MAX_PROMPT_IMAGES = 3


class SourceLookupUnavailableError(Exception):
    """Raised when we can't tell whether a ticket has an implementation.

    `require_source_grounding` refuses to write a plan with no PR behind it,
    and says so in words — "No implementation was found for this ticket …
    link a pull request". That sentence is only true if the lookup actually
    ran. When Jira's dev-status endpoint times out or rate-limits, the honest
    answer is that we don't know, so this fails the run as the transient
    error it is instead of publishing a confident falsehood about someone's
    ticket. The watcher retries on its next sweep; the UI says to try again.
    """

    def __init__(self, ticket_key: str):
        self.ticket_key = ticket_key
        super().__init__(
            f"Could not reach Jira's dev-status API for {ticket_key}, so whether "
            "it has a linked pull request is unknown. No plan was generated — "
            "this is a temporary Jira failure, not a missing implementation. "
            "Try again in a moment."
        )


class NonTestableIssueError(Exception):
    """Raised for issue types the bot deliberately won't plan for (Epic, Spike).

    Carries the offending type and key so the route layer can build the
    same 400 message it always did.
    """

    def __init__(self, issue_type: str, ticket_key: str | None = None):
        self.issue_type = issue_type
        self.ticket_key = ticket_key
        if ticket_key:
            message = (
                f"Test plans are not generated for {issue_type} issues ({ticket_key})."
            )
        else:
            message = (
                f"Test plans are not generated for {issue_type} issues. "
                f"Only Story, Task, and Bug issues are supported."
            )
        super().__init__(message)


# --------------------------------------------------------------------------
# Jira issue → payload serialization
#
# One serializer, two shapes. ``serialize_issue`` is what the UI reads
# from ``GET /issue/{key}``; ``prompt_payload`` narrows that to the
# fields generation actually consumes. Keeping the second derived from
# the first is what guarantees a server-side generate sees exactly the
# context a browser-side generate did.
# --------------------------------------------------------------------------


def serialize_issue(issue) -> dict:
    """Serialize a ``JiraClient.get_issue()`` result into the ticket dict the
    frontend consumes. Extracted from the ``GET /issue/{key}`` handler so the
    server can rebuild a generate payload without a browser round-trip.
    """
    development_info_dict = None
    if issue.development_info:
        development_info_dict = {
            "commits": [asdict(commit) for commit in issue.development_info.commits],
            "pull_requests": [
                asdict(pr) for pr in issue.development_info.pull_requests
            ],
            "branches": issue.development_info.branches,
            "repository_context": (
                asdict(issue.development_info.repository_context)
                if issue.development_info.repository_context
                else None
            ),
            "figma_context": (
                asdict(issue.development_info.figma_context)
                if issue.development_info.figma_context
                else None
            ),
        }

    attachments_list = None
    if issue.attachments:
        attachments_list = [asdict(attachment) for attachment in issue.attachments]

    comments_list = None
    if issue.comments:
        comments_list = [asdict(comment) for comment in issue.comments]

    parent_info_dict = None
    if issue.parent:
        parent_info_dict = {
            "key": issue.parent.key,
            "summary": issue.parent.summary,
            "description": issue.parent.description,
            "issue_type": issue.parent.issue_type,
            "labels": issue.parent.labels,
        }
        if issue.parent.attachments:
            parent_info_dict["attachments"] = [
                asdict(att) for att in issue.parent.attachments
            ]
        if issue.parent.figma_context:
            parent_info_dict["figma_context"] = asdict(issue.parent.figma_context)

    # Direct sub-tasks / Epic children. Their presence switches the prompt
    # into parent/integration-test mode.
    children_list = None
    if issue.children:
        children_list = [asdict(child) for child in issue.children]

    linked_info_dict = None
    if issue.linked_issues:
        linked_info_dict = {}
        if issue.linked_issues.blocks:
            linked_info_dict["blocks"] = [
                asdict(link) for link in issue.linked_issues.blocks
            ]
        if issue.linked_issues.blocked_by:
            linked_info_dict["blocked_by"] = [
                asdict(link) for link in issue.linked_issues.blocked_by
            ]
        if issue.linked_issues.causes:
            linked_info_dict["causes"] = [
                asdict(link) for link in issue.linked_issues.causes
            ]
        if issue.linked_issues.caused_by:
            linked_info_dict["caused_by"] = [
                asdict(link) for link in issue.linked_issues.caused_by
            ]

    bounce_history_list = None
    if issue.bounce_history:
        bounce_history_list = [asdict(b) for b in issue.bounce_history]

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
        "description_quality": {
            "has_description": issue.description_analysis.has_description,
            "gaps": issue.description_analysis.gaps,
            "char_count": issue.description_analysis.char_count,
            "word_count": issue.description_analysis.word_count,
        },
        "development_info": development_info_dict,
        "dev_status_unavailable": getattr(issue, "dev_status_unavailable", False),
        "attachments": attachments_list,
        "comments": comments_list,
        "parent": parent_info_dict,
        "children": children_list,
        "linked_issues": linked_info_dict,
        "status": issue.status,
        "status_category": issue.status_category,
        "bounce_history": bounce_history_list,
        "story_points": issue.story_points,
    }


def prompt_payload(serialized: dict) -> dict:
    """Narrow a ``serialize_issue`` dict to the generate-request shape.

    Mirrors ``buildTicketPayload`` in ``frontend/src/hooks/useTestPlan.js``
    field for field — that parity is the point, since it's what makes a
    server-side generate produce the same plan the UI would.
    """
    attachments = serialized.get("attachments") or []
    return {
        "ticket_key": serialized["key"],
        "summary": serialized["summary"],
        "description": serialized.get("description"),
        "issue_type": serialized["issue_type"],
        "testing_context": {},
        "development_info": serialized.get("development_info"),
        "dev_status_unavailable": bool(serialized.get("dev_status_unavailable")),
        "image_urls": [a["url"] for a in attachments if a.get("url")] or None,
        "comments": serialized.get("comments") or None,
        "parent_info": serialized.get("parent") or None,
        "child_info": serialized.get("children") or None,
        "linked_info": serialized.get("linked_issues") or None,
        "bounce_history": serialized.get("bounce_history") or None,
    }


async def fetch_ticket_payload(ticket_key: str, *, jira: JiraClient | None = None) -> dict:
    """Fetch `ticket_key` from Jira and return its generate-request payload.

    Propagates the ``Jira*Error`` family untouched so callers can decide
    whether a missing ticket is a 404, a CLI error message, or a skipped
    row in a batch.
    """
    client = jira or JiraClient()
    issue = await client.get_issue(ticket_key)
    return prompt_payload(serialize_issue(issue))


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


async def _download_images(
    image_urls: list[str] | None, *, jira: JiraClient | None = None
) -> list | None:
    """Download up to ``MAX_PROMPT_IMAGES`` attachments as base64 for the prompt.

    Returns None (not an empty list) when nothing downloadable was found,
    matching what the LLM clients expect for "no images".
    """
    if not image_urls:
        return None
    client = jira or JiraClient()
    images: list = []
    for image_url in image_urls[:MAX_PROMPT_IMAGES]:
        image_data = await client.download_image_as_base64(image_url)
        if image_data:
            images.append(image_data)
    return images or None


async def _load_seed_regressions(ticket_key: str, parent_key: str) -> list[dict]:
    """Prior Bug Lens regression tests from sibling tickets under the same parent.

    Best-effort: a DB hiccup here degrades the plan's seeding, not the plan.
    """
    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            seeds = await bug_analysis_repository.find_seed_regression_tests(
                session,
                ticket_key=ticket_key,
                parent_key=parent_key,
                limit=5,
            )
        seed_count = sum(len(s.get("regression_tests") or []) for s in seeds)
        logger.info(
            "seed_regressions: ticket=%s parent=%s sources=%d total_tests=%d",
            ticket_key,
            parent_key,
            len(seeds),
            seed_count,
        )
        return seeds
    except Exception:
        logger.exception("find_seed_regression_tests failed; continuing without seeds")
        return []


async def generate_single(
    request: GenerateTestPlanRequest, *, llm=None
) -> dict:
    """Generate, critique, score and persist a single-ticket test plan.

    Raises ``NonTestableIssueError`` for Epic/Spike and lets ``LLMError``
    propagate; both are recorded against the run before they escape.
    """
    if request.issue_type in NON_TESTABLE_ISSUE_TYPES:
        raise NonTestableIssueError(request.issue_type)

    # Resolve what this run is allowed to ground cases in, BEFORE anything
    # reads development_info. Closed-unmerged PRs are dropped here and never
    # reach the prompt or the critics; provenance keeps a record of them so
    # the plan can say what was skipped and why.
    filtered_dev_info, provenance = filter_development_info(
        request.development_info
    )
    request = request.model_copy(update={"development_info": filtered_dev_info})

    flags = derive_context_flags(request)
    parent_key = (request.parent_info or {}).get("key")
    parent_key_clean = (
        parent_key if isinstance(parent_key, str) and parent_key.strip() else None
    )
    run_ctx = await run_tracker.start_run(
        run_type=RunType.test_plan,
        ticket_keys=[request.ticket_key],
        model=settings.llm_model,
        llm_provider=settings.llm_provider,
        ticket_title=request.summary,
        ticket_issue_type=request.issue_type,
        ticket_parent_key=parent_key_clean,
        source_provenance=provenance,
        **flags,
    )

    # No merged and no open PR means there is nothing to write cases
    # against. Emitting a plan anyway is the SK-2609 failure: ten cases of
    # speculation that read as authoritative and cost a tester a cycle
    # each. Say what was searched instead, and ask for a PR or a spec.
    if settings.require_source_grounding and not has_grounding_source(provenance):
        if request.dev_status_unavailable:
            provenance["dev_status_unavailable"] = True
            logger.warning(
                "source lookup unavailable for %s; refusing to report it as "
                "having no implementation",
                request.ticket_key,
            )
            await run_tracker.fail(run_ctx, error_code="dev_status_unavailable")
            raise SourceLookupUnavailableError(request.ticket_key)
        logger.info(
            "no_source: %s has no merged or open PR (%d closed-unmerged skipped); "
            "returning a no-implementation result instead of a plan",
            request.ticket_key,
            provenance.get("excluded_pr_count", 0),
        )
        await run_tracker.complete(run_ctx)
        return no_source_result(request.ticket_key, provenance)

    seed_regressions: list[dict] = []
    if parent_key_clean:
        seed_regressions = await _load_seed_regressions(
            request.ticket_key, parent_key_clean
        )

    try:
        images = await _download_images(request.image_urls)

        resolved_slack = await resolve_slack_messages_in_text(
            request.description, request.comments
        )
        slack_messages_for_prompt = (
            [asdict(m) for m in resolved_slack] if resolved_slack else None
        )

        llm = llm or get_llm_client()

        # Pre-plan classifier — name the deliverable + verification surface
        # so the generator can anchor cases to the right target and the
        # post-plan surface critic has something to check against. Gated
        # by settings.surface_classifier_enabled; off means the pipeline
        # runs exactly as it did before this step existed.
        deliverable = await classify_deliverable(
            llm,
            ticket_key=request.ticket_key,
            summary=request.summary,
            description=request.description,
            issue_type=request.issue_type,
            development_info=request.development_info,
        )
        testing_context = dict(request.testing_context or {})
        if deliverable is not None and deliverable.artifact_type != "unknown":
            testing_context["deliverable_hint"] = format_deliverable_hint(deliverable)

        test_plan = await llm.generate_test_plan(
            ticket_key=request.ticket_key,
            summary=request.summary,
            description=request.description,
            testing_context=testing_context,
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

        # AC coverage for the single-ticket plan. Previously only the
        # multi-ticket endpoint computed this, so a plain Story (no children)
        # had no coverage safety net at all — the SK-2290 failure, where
        # dropped AC items shipped silently. Build the same per-ticket index
        # the multi-ticket path uses so compute_ac_coverage can flag both
        # fully-uncovered ACs and compound ACs whose sub-actions were dropped.
        single_ticket_data = [
            {
                "ticket_key": request.ticket_key,
                "acceptance_criteria": extract_acceptance_criteria(request.description),
            }
        ]
        # Grounding critic — catch cases that cite an AC by number but test a
        # behaviour that AC's text doesn't actually contain (e.g. a "filter by
        # date range" case tagged against an AC that only says "viewable").
        # Runs before compute_ac_coverage so any badges added here survive
        # the covers_acs cleanup pass.
        await run_grounding_critic(llm, test_plan, single_ticket_data)
        # Code-grounding recheck — for each just-added AC-critic warning,
        # look at the linked repo's source and downgrade the warning to
        # INFO when the behaviour under test is demonstrably implemented.
        # Runs immediately after the AC critic so fresh warnings can be
        # softened before the fix-scope critic keys off them.
        single_ticket_dev_info = [
            {
                "ticket_key": request.ticket_key,
                "development_info": request.development_info,
            }
        ]
        await run_code_grounding_critic(llm, test_plan, single_ticket_dev_info)
        # Fix-scope critic — catch reporter-drift cases that cite a real AC
        # but test behaviour the merged PR explicitly did NOT change.
        # Ordered after the grounding critic so already-badged cases skip
        # the second LLM call.
        await run_fix_scope_critic(llm, test_plan, single_ticket_dev_info)
        # Surface-mismatch critic — for tickets whose deliverable lives
        # OUTSIDE the running app (asset upload, config flip, doc edit),
        # badge cases whose steps target the app anyway. Skipped for
        # code_behavior / unknown / classifier-off (see should_run).
        await run_surface_mismatch_critic(llm, test_plan, deliverable)
        # Quarantine last: the code-grounding critic un-badges cases whose
        # behaviour it found in the repo, and those rescues have to land
        # before we decide what is ungrounded.
        needs_spec_cases = quarantine_ungrounded_cases(test_plan)
        ac_coverage = compute_ac_coverage(test_plan, single_ticket_data)

        response = {
            "ticket_key": request.ticket_key,
            "happy_path": test_plan.happy_path,
            "edge_cases": test_plan.edge_cases,
            "regression_checklist": test_plan.regression_checklist,
            "integration_tests": test_plan.integration_tests or [],
            "needs_spec_cases": needs_spec_cases,
            "ac_coverage": ac_coverage,
            "grounding_warnings": normalize_grounding_warnings(test_plan),
            "risks_and_gaps": test_plan.risks_and_gaps or [],
            "uat_complexity": test_plan.uat_complexity,
            "how_to_see_it": test_plan.how_to_see_it,
            "source_provenance": provenance,
        }

        saved = await run_tracker.complete_with_plan(
            run_ctx,
            plan_body=json.dumps(response),
            plan_format=PlanFormat.json,
            cases=flatten_cases_for_persistence(test_plan),
        )
        if saved:
            response["plan_id"] = saved["plan_id"]
            response["version"] = saved["version"]
        return response

    except Exception as e:
        await run_tracker.fail(run_ctx, error_code=_error_code(e))
        raise


async def generate_multi(tickets: list[TicketInput], *, llm=None) -> dict:
    """Generate a unified plan across several related tickets.

    Two modes:

    * **single_repo** — all tickets touch the same repository (or carry no
      development info). One unified plan keyed off shared files.
    * **cross_project** — tickets span repositories. The seam extractor
      finds verified producer/consumer pairs across repos and feeds them
      into the prompt so the model writes integration tests against the
      seams rather than per-side behaviour.
    """
    for ticket in tickets:
        if ticket.issue_type in NON_TESTABLE_ISSUE_TYPES:
            raise NonTestableIssueError(ticket.issue_type, ticket.ticket_key)

    # Same state filter the single-ticket path applies, per ticket, before
    # anything downstream reads development_info.
    per_ticket_provenance: list[tuple[str, dict]] = []
    filtered_tickets: list[TicketInput] = []
    for ticket in tickets:
        filtered_dev_info, prov = filter_development_info(ticket.development_info)
        per_ticket_provenance.append((ticket.ticket_key, prov))
        filtered_tickets.append(
            ticket.model_copy(update={"development_info": filtered_dev_info})
        )
    tickets = filtered_tickets
    provenance = merge_provenance(per_ticket_provenance)

    aggregated_flags = {
        "had_pr_diff": False,
        "had_figma": False,
        "had_parent": False,
        "linked_ticket_count": 0,
        "pr_count": 0,
        "comment_count": 0,
    }
    for t in tickets:
        flags = derive_context_flags(t)
        aggregated_flags["had_pr_diff"] = (
            aggregated_flags["had_pr_diff"] or flags["had_pr_diff"]
        )
        aggregated_flags["had_figma"] = aggregated_flags["had_figma"] or flags["had_figma"]
        aggregated_flags["had_parent"] = (
            aggregated_flags["had_parent"] or flags["had_parent"]
        )
        aggregated_flags["linked_ticket_count"] += flags["linked_ticket_count"]
        aggregated_flags["pr_count"] += flags["pr_count"]
        aggregated_flags["comment_count"] += flags["comment_count"]

    run_ctx = await run_tracker.start_run(
        run_type=RunType.test_plan,
        ticket_keys=[t.ticket_key for t in tickets],
        model=settings.llm_model,
        llm_provider=settings.llm_provider,
        source_provenance=provenance,
        **aggregated_flags,
    )

    # Nothing in the batch is grounded in code that exists. A unified plan
    # across several unimplemented tickets is the same speculation as a
    # single-ticket one, multiplied.
    if settings.require_source_grounding and not has_grounding_source(provenance):
        unknown = [t.ticket_key for t in tickets if t.dev_status_unavailable]
        if unknown:
            # One unchecked ticket is enough: the batch can't be called
            # unimplemented when we never found out about part of it.
            provenance["dev_status_unavailable"] = True
            logger.warning(
                "source lookup unavailable for %s; refusing to report the batch "
                "as having no implementation",
                unknown,
            )
            await run_tracker.fail(run_ctx, error_code="dev_status_unavailable")
            raise SourceLookupUnavailableError(", ".join(unknown))
        logger.info(
            "no_source: none of %s has a merged or open PR; "
            "returning a no-implementation result instead of a plan",
            [t.ticket_key for t in tickets],
        )
        await run_tracker.complete(run_ctx)
        result = no_source_result(tickets[0].ticket_key, provenance)
        result["ticket_keys"] = [t.ticket_key for t in tickets]
        return result

    try:
        # Images are pooled across tickets and capped in total, not per
        # ticket — three from the first ticket means none from the rest.
        all_images: list | None = None
        jira = JiraClient()
        for ticket in tickets:
            if ticket.image_urls and (
                all_images is None or len(all_images) < MAX_PROMPT_IMAGES
            ):
                for url in ticket.image_urls:
                    if all_images is not None and len(all_images) >= MAX_PROMPT_IMAGES:
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
                # Carried through so _build_multi_ticket_prompt can render the
                # bounce-back section. Omitting this key was why multi-ticket
                # plans never covered prior QA/UAT failure modes.
                "bounce_history": t.bounce_history,
                "acceptance_criteria": extract_acceptance_criteria(t.description),
            }
            for t in tickets
        ]

        mode = classify_multi_ticket_mode(tickets_data)
        cross_project_payload: dict | None = None
        if mode == "cross_project":
            catalog = build_seam_catalog(tickets_data)
            if not catalog.is_empty:
                cross_project_payload = catalog.to_dict()

        llm = llm or get_llm_client()

        # Pre-plan classifier — one call per ticket in parallel. Each ticket's
        # non-code deliverable hint is attached into its own testing_context so
        # the multi prompt anchors that ticket's block without leaking hints
        # across sibling tickets in the batch. The aggregated deliverable is
        # then handed to the surface critic after generation (skipped when the
        # batch contains any code_behavior ticket — see
        # aggregate_deliverables_for_critique for why).
        classifier_results = await asyncio.gather(
            *[
                classify_deliverable(
                    llm,
                    ticket_key=t["ticket_key"],
                    summary=t.get("summary") or "",
                    description=t.get("description") or "",
                    issue_type=t.get("issue_type"),
                    development_info=t.get("development_info"),
                )
                for t in tickets_data
            ],
            return_exceptions=False,
        )
        per_ticket_deliverables: list[tuple[str, object]] = []
        for t, d in zip(tickets_data, classifier_results):
            per_ticket_deliverables.append((t["ticket_key"], d))
            if d is not None and getattr(d, "artifact_type", "unknown") != "unknown":
                tc = dict(t.get("testing_context") or {})
                tc["deliverable_hint"] = format_deliverable_hint(d)
                t["testing_context"] = tc
        aggregated_deliverable = aggregate_deliverables_for_critique(
            per_ticket_deliverables
        )

        test_plan = await llm.generate_multi_ticket_test_plan(
            tickets=tickets_data,
            images=all_images,
            cross_project=cross_project_payload,
        )

        # Critics run in the same order as the single-ticket path; see
        # generate_single for what each one catches.
        await run_grounding_critic(llm, test_plan, tickets_data)
        multi_ticket_dev_info = [
            {
                "ticket_key": t.get("ticket_key"),
                "development_info": t.get("development_info"),
            }
            for t in tickets_data
        ]
        await run_code_grounding_critic(llm, test_plan, multi_ticket_dev_info)
        await run_fix_scope_critic(llm, test_plan, multi_ticket_dev_info)
        # Surface-mismatch critic — runs against the aggregated deliverable.
        # Skipped for pure-code batches or when the batch mixes code with
        # non-code work (see aggregate_deliverables_for_critique). This
        # matches the single-ticket path so the two behave symmetrically.
        await run_surface_mismatch_critic(llm, test_plan, aggregated_deliverable)
        # See generate_single: quarantining runs after every critic so the
        # code-grounding recheck's rescues are respected.
        needs_spec_cases = quarantine_ungrounded_cases(test_plan)
        ac_coverage = compute_ac_coverage(test_plan, tickets_data)
        valid_ac_ids = {
            f"{t['ticket_key']}-AC{i}"
            for t in tickets_data
            for i in range(1, len(t.get("acceptance_criteria") or []) + 1)
        }
        grounding_warnings = normalize_grounding_warnings(test_plan, valid_ac_ids)

        response = {
            "ticket_keys": [t.ticket_key for t in tickets],
            "happy_path": test_plan.happy_path,
            "edge_cases": test_plan.edge_cases,
            "regression_checklist": test_plan.regression_checklist,
            "integration_tests": test_plan.integration_tests or [],
            "needs_spec_cases": needs_spec_cases,
            "ac_coverage": ac_coverage,
            "superseded_acs": ac_coverage.get("superseded_acs", []),
            "grounding_warnings": grounding_warnings,
            "risks_and_gaps": test_plan.risks_and_gaps or [],
            "uat_complexity": test_plan.uat_complexity,
            "how_to_see_it": test_plan.how_to_see_it,
            "source_provenance": provenance,
        }
        if cross_project_payload is not None:
            response["cross_project_summary"] = (
                test_plan.cross_project_summary or cross_project_payload
            )

        saved = await run_tracker.complete_with_plan(
            run_ctx,
            plan_body=json.dumps(response),
            plan_format=PlanFormat.json,
            cases=flatten_cases_for_persistence(test_plan),
        )
        if saved:
            response["plan_id"] = saved["plan_id"]
            response["version"] = saved["version"]
        return response

    except Exception as e:
        await run_tracker.fail(run_ctx, error_code=_error_code(e))
        raise


def _error_code(exc: Exception) -> str:
    """Format an exception for ``runs.error_code``.

    ``LLMError`` is recorded by name-and-message (it's the expected
    failure and the message is the useful part); anything else gets its
    type prefixed so unexpected classes are greppable in the runs table.
    """
    if isinstance(exc, LLMError):
        return f"LLMError: {exc}"
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# Key-only entry points — the door for CLI / MCP / background callers
# --------------------------------------------------------------------------


async def generate_for_ticket(ticket_key: str) -> dict:
    """Fetch `ticket_key` from Jira and generate its plan, critics and all.

    The one-call equivalent of what the browser does across
    ``GET /issue/{key}`` + ``POST /generate-test-plan``.
    """
    payload = await fetch_ticket_payload(ticket_key)
    return await generate_single(GenerateTestPlanRequest(**payload))


async def generate_for_tickets(ticket_keys: list[str]) -> dict:
    """Fetch several tickets and generate one unified plan across them.

    A single key routes to the single-ticket pipeline so callers don't
    have to branch — the two produce differently shaped responses
    (``ticket_key`` vs ``ticket_keys``), which is the existing API
    contract and not something this function papers over.
    """
    if not ticket_keys:
        raise ValueError("generate_for_tickets requires at least one ticket key")
    if len(ticket_keys) == 1:
        return await generate_for_ticket(ticket_keys[0])

    jira = JiraClient()
    payloads = await asyncio.gather(
        *[fetch_ticket_payload(key, jira=jira) for key in ticket_keys]
    )
    return await generate_multi([TicketInput(**p) for p in payloads])
