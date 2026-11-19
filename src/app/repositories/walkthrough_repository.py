from __future__ import annotations

import json

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.app.db import crud
from src.app.db.models.ticket_walkthrough import TicketWalkthrough


async def get_walkthrough(
    db: AsyncIOMotorDatabase,
    *,
    ticket_key: str,
) -> TicketWalkthrough | None:
    """Return the walkthrough document for `ticket_key`, or None if none exists."""
    return await crud.find_one(db, TicketWalkthrough, {"ticket_key": ticket_key.upper()})


def decode_screenshots(row: TicketWalkthrough | None) -> list[dict]:
    """Decode a walkthrough document's ``screenshots`` JSON blob into a list.

    Returns [] when the document is missing, the field is empty, or the stored
    value fails to parse into a list of ``{filename, url, media_id?}``
    objects — the walkthrough is optional metadata, so a bad blob should
    never break the ticket page. ``media_id`` is optional (legacy documents
    saved before inline rendering shipped won't have one; the workflow
    route re-resolves those from the URL at comment time).
    """
    if row is None or not row.screenshots:
        return []
    try:
        parsed = json.loads(row.screenshots)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    cleaned: list[dict] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        if not url:
            continue
        filename = (entry.get("filename") or "screenshot").strip() or "screenshot"
        media_id_raw = entry.get("media_id")
        media_id = (
            media_id_raw.strip() if isinstance(media_id_raw, str) and media_id_raw.strip()
            else None
        )
        cleaned.append({"filename": filename, "url": url, "media_id": media_id})
    return cleaned


# Why `needs_walkthrough` came out the way it did. The 409 detail and the
# readiness response both carry it so the UI can say why instead of guessing.
REASON_NOT_HIGH = "not_high_complexity"
REASON_PRESENT = "walkthrough_present"
REASON_BACKEND_ONLY = "backend_only"
REASON_TEXT_EVIDENCE = "evidence_attached"
REASON_UI = "ui_needs_walkthrough"
REASON_MANUAL = "manual_checks_need_evidence"
REASON_UNKNOWN = "surface_unknown"
REASON_UNCHECKED = "attachments_unchecked"


def walkthrough_sources(walkthrough: dict) -> list[str]:
    """Which walkthrough-card fields carry material."""
    sources: list[str] = []
    if walkthrough.get("loom_url"):
        sources.append("loom")
    shots = walkthrough.get("screenshots") or []
    if isinstance(shots, list) and len(shots) > 0:
        sources.append("screenshots")
    notes = walkthrough.get("notes")
    if isinstance(notes, str) and notes.strip():
        sources.append("notes")
    return sources


def attachments_could_decide(
    walkthrough: dict, uat_complexity: str | None, uat_surface: str | None
) -> bool:
    """True when the ticket's Jira attachments would change the decision —
    the only case worth a Jira round trip."""
    return (
        uat_complexity == "high"
        and uat_surface == "manual"
        and not walkthrough_sources(walkthrough)
    )


def derive_readiness(
    walkthrough: dict,
    uat_complexity: str | None,
    *,
    uat_surface: str | None = None,
    evidence_attachments: list[dict] | None = (),
) -> dict:
    """Compute the UAT-readiness signal from a serialized walkthrough.

    Single source of truth for "does this ticket have walkthrough material?" —
    both the walkthrough card and the Pass-to-UAT gate historically re-derived
    this rule in JS and drifted. Returning it from the API keeps them aligned
    and lets the Pass-to-UAT route enforce the same rule server-side.

    ``sources`` names which fields carry material — useful for UI hints like
    "Notes and screenshots attached" without the caller re-inspecting it.

    ``uat_surface`` comes from ``src.app.uat_surface.classify``:
      - ``backend``: every case is an API call or a terminal command. There is
        nothing to film, so no walkthrough is needed (SK-3014).
      - ``manual``: no screen cases but some manual checks. Written evidence
        counts: the card's notes, or ``evidence_attachments`` (non-plan Jira
        attachments added after the plan; ``None`` means Jira could not be
        asked, which is reported as unchecked rather than as missing).
      - ``ui``, ``mixed``, ``unknown`` or absent: the gate is unchanged.
    """
    sources = walkthrough_sources(walkthrough)
    present = len(sources) > 0
    if uat_complexity != "high":
        needs, reason = False, REASON_NOT_HIGH
    elif present:
        needs, reason = False, REASON_PRESENT
    elif uat_surface == "backend":
        needs, reason = False, REASON_BACKEND_ONLY
    elif uat_surface == "manual":
        if evidence_attachments is None:
            needs, reason = True, REASON_UNCHECKED
        elif evidence_attachments:
            needs, reason = False, REASON_TEXT_EVIDENCE
        else:
            needs, reason = True, REASON_MANUAL
    elif uat_surface in ("ui", "mixed"):
        needs, reason = True, REASON_UI
    else:
        needs, reason = True, REASON_UNKNOWN
    return {
        "walkthrough_present": present,
        "walkthrough_sources": sources,
        "needs_walkthrough": needs,
        "walkthrough_reason": reason,
    }


async def upsert_walkthrough(
    db: AsyncIOMotorDatabase,
    *,
    ticket_key: str,
    loom_url: str | None,
    notes: str | None,
    screenshots: list[dict],
) -> TicketWalkthrough:
    """Create or update the single walkthrough document for ``ticket_key``.

    ``screenshots`` is the desired final list — pass ``[]`` to clear all
    attached screenshots. Each entry must have ``url`` (required) and
    ``filename`` (defaulted to ``"screenshot"`` when blank). Entries may
    also carry ``media_id`` — the Atlassian media-services UUID used to
    render the screenshot inline in the Pass-to-UAT comment. Absent
    ``media_id`` falls back to a `📷 <filename>` text callout when the
    comment is built.
    """
    key = ticket_key.upper()
    row = await get_walkthrough(db, ticket_key=key)
    if row is None:
        row = TicketWalkthrough(ticket_key=key)
    row.loom_url = loom_url or None
    row.notes = notes or None
    normalized: list[dict] = []
    for entry in screenshots or []:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        if not url:
            continue
        filename = (entry.get("filename") or "").strip() or "screenshot"
        media_id_raw = entry.get("media_id")
        media_id = (
            media_id_raw.strip() if isinstance(media_id_raw, str) and media_id_raw.strip()
            else None
        )
        record: dict = {"filename": filename, "url": url}
        if media_id:
            record["media_id"] = media_id
        normalized.append(record)
    row.screenshots = json.dumps(normalized) if normalized else None
    if row.id is None:
        return await crud.insert(db, row)
    return await crud.save(db, row)
