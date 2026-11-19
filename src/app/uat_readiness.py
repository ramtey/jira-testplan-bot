"""UAT readiness service — composes the walkthrough row and the latest plan's
UAT complexity into a single decision the API responses and the Pass-to-UAT
gate both consume. Kept out of the repositories package so it can depend on
`walkthrough_repository` and `plan_repository` without creating a circular
import through `repositories/__init__.py`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.app import uat_surface
from src.app.repositories import plan_repository, walkthrough_repository

logger = logging.getLogger(__name__)

# Lists a ticket's Jira attachments. Injected so tests never reach Jira.
AttachmentLoader = Callable[[str], Awaitable[list[dict]]]

# The bot's own uploads of the plan behind an index comment
# (`jira_client.plan_attachment_filename`). They are on every posted ticket, so
# counting them as QA evidence would wave every ticket through.
_PLAN_ATTACHMENT_RE = re.compile(r"^(batch-)?test-plan-[A-Z][A-Z0-9_]*-\d+\.md$", re.I)


async def fetch_readiness(
    db: AsyncIOMotorDatabase,
    *,
    ticket_key: str,
    load_attachments: AttachmentLoader | None = None,
) -> dict:
    """Return the enriched walkthrough shape for a ticket.

    Shape:
        {
          "loom_url", "screenshots", "notes", "updated_at",
          "uat_complexity", "uat_surface",
          "walkthrough_present", "walkthrough_sources", "needs_walkthrough",
          "walkthrough_reason", "evidence_attachments",
        }

    Used by GET/PUT `/tickets/{key}/walkthrough` and by the Pass-to-UAT gate
    on the workflow route — computing it in one place stops the response
    surface and the enforcement rule from drifting apart.

    Jira is asked for the ticket's attachments only when the answer can change
    the decision: a high-complexity ``manual`` plan with nothing on the
    walkthrough card. Everything else is decided from the database alone.
    """
    row = await walkthrough_repository.get_walkthrough(
        db, ticket_key=ticket_key
    )
    data = _serialize_walkthrough(row)
    complexity, surface, plan_created_at = await _fetch_plan_facts(db, ticket_key)
    data["uat_complexity"] = complexity
    data["uat_surface"] = surface

    evidence: list[dict] | None = []
    if walkthrough_repository.attachments_could_decide(data, complexity, surface):
        evidence = await _load_evidence_attachments(
            ticket_key, plan_created_at, load_attachments
        )
    data["evidence_attachments"] = evidence or []
    data.update(
        walkthrough_repository.derive_readiness(
            data, complexity, uat_surface=surface, evidence_attachments=evidence
        )
    )
    return data


def _serialize_walkthrough(row) -> dict:
    if row is None:
        return {
            "loom_url": None,
            "screenshots": [],
            "notes": None,
            "updated_at": None,
        }
    return {
        "loom_url": row.loom_url,
        "screenshots": walkthrough_repository.decode_screenshots(row),
        "notes": row.notes,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _fetch_plan_facts(
    db: AsyncIOMotorDatabase, ticket_key: str
) -> tuple[str | None, str | None, datetime | None]:
    """``(uat_complexity, uat_surface, created_at)`` of the latest plan."""
    latest = await plan_repository.find_latest_plan_for_ticket(
        db, ticket_key=ticket_key.upper()
    )
    if not latest or not latest.body:
        return None, None, None
    try:
        body = json.loads(latest.body) or {}
    except (ValueError, TypeError):
        return None, None, None
    if not isinstance(body, dict):
        return None, None, None
    return body.get("uat_complexity"), uat_surface.classify(body), latest.created_at


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _parse_jira_time(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    # Jira writes `2026-11-18T10:23:45.123-0800`; fromisoformat wants `-08:00`.
    fixed = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", raw.strip())
    try:
        return _as_utc(datetime.fromisoformat(fixed.replace("Z", "+00:00")))
    except ValueError:
        return None


def evidence_from_attachments(
    attachments: list[dict], plan_created_at: datetime | None
) -> list[dict]:
    """The attachments that count as QA evidence: anything a person attached
    after the plan was written, except the bot's own plan file.

    The time cut keeps a spec or a design export attached when the ticket was
    filed from passing as test results. Its cost: a plan regenerated after the
    evidence went up hides that evidence, and the tester adds notes or uses the
    override. An attachment with no readable timestamp is not counted.
    """
    cutoff = _as_utc(plan_created_at) if plan_created_at else None
    out: list[dict] = []
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        name = (att.get("filename") or "").strip()
        if not name or _PLAN_ATTACHMENT_RE.match(name):
            continue
        created = _parse_jira_time(att.get("created"))
        if cutoff is not None and (created is None or created < cutoff):
            continue
        out.append({
            "filename": name,
            "mime_type": att.get("mimeType") or None,
            "created": att.get("created") or None,
        })
    return out


async def _default_attachment_loader(ticket_key: str) -> list[dict]:
    # Imported here: jira_client is heavy and most readiness calls never get here.
    from src.app.jira_client import JiraClient

    return await JiraClient().get_attachments(ticket_key)


async def _load_evidence_attachments(
    ticket_key: str,
    plan_created_at: datetime | None,
    load_attachments: AttachmentLoader | None,
) -> list[dict] | None:
    """Evidence attachments, or None when Jira could not be asked. None is
    not "no evidence": the caller reports it as unchecked."""
    loader = load_attachments or _default_attachment_loader
    try:
        attachments = await loader(ticket_key.upper())
    except Exception:
        logger.warning(
            "Could not list Jira attachments for %s; walkthrough evidence unchecked",
            ticket_key,
            exc_info=True,
        )
        return None
    return evidence_from_attachments(attachments, plan_created_at)
