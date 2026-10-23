"""Regenerating a plan without losing what QA already recorded against it.

QA progress is stored under a key built from the plan's section counts (for
example ``SK-2822:5-9-5-15``), and a case is identified only by
``<section>:<index>``. A regeneration that moves, drops or re-sections a case
therefore re-points every recorded result after it, and nothing errors:

* if the counts change, the marks sit under the old key and the new plan
  starts at 0% (``mark_carryover`` is the way back, case by case);
* if the counts happen to come out the same, the new plan reads the old row
  directly, and each tick lands on whatever case now sits at that position.
  ``mark_carryover`` cannot see this, because it only looks at rows under
  *other* keys.

So a regeneration now does two things:

1. **Before generating**, the previous single-ticket plan and its recorded
   marks go into the prompt (``render_previous_plan``), so the model can reword
   a case in place instead of rewriting the plan around it. Rule I still applies.
   The old plan's reasons are re-checked, not inherited.
2. **After generating**, ``build_progress_remap`` maps every old id to the new
   plan's id from the final bodies. Those are what the key is computed from,
   after quarantine and coverage have moved cases the model never moved. The
   result goes on the plan as ``progress_remap``: the old key, the new key, the
   id mapping, and the recorded marks it affects. The response carries it, so
   whoever posts the plan sees the mapping first.

Nothing here writes progress. Carrying a mark across is still
``mark_carryover.apply``, for the ids someone confirms.
"""

from __future__ import annotations

import re
from typing import Any

from src.app.services import progress_key as progress_key_service
from src.app.services.mark_carryover import _similarity

# Text that later pipeline stages add to a case. It says nothing about which
# test the case is, and on a short regression line it can outweigh the words
# that do, so it is stripped before comparing.
_ADDED_BY_CODE = re.compile(
    r"Deploy check first: confirm the environment runs merged commit \S+ or later\.\s*"
    # build_targets.short_requirement; a dot before a digit is a version's.
    r"|Build check first: confirm (?:[^.]|\.(?=\d))*\.\s*"
    r"|Unverified — assumption(?: \([^)]*\))?:\s*"
    r"|Pre-existing behaviour, not changed by this ticket:\s*"
    r"|Platform-dependent — [^:]*:\s*"
    r"|Check scope — [^:]*:\s*"
    r"|From testing note \S+:?\s*"
    r"|[🔴🟡🟢]\s*",
    re.I,
)
_WORD = re.compile(r"[^a-z0-9]+")
_EXPECTED_CHARS = 280

# Calibrated on SK-2822's 620 -> 622 regeneration. Character-sequence similarity
# (what mark_carryover uses on titles) collapses when a regression line gains or
# loses a code-added prefix: the old and new deploy-check lines scored 0.05.
# Shared words do not care about order. Every true pair there scored >= 0.53 on
# Dice and the best wrong candidate 0.49, so a match below CONFIDENT is reported
# for a person to read rather than trusted.
MATCH_THRESHOLD = 0.5
CONFIDENT = 0.6


def _norm(text: str) -> str:
    return _WORD.sub(" ", _ADDED_BY_CODE.sub("", text or "").lower()).strip()


def _short(text: str, limit: int = _EXPECTED_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def protected_title(title: str) -> str:
    """How quarantine recognises a case QA already passed: its normalised title."""
    return _norm(title)


def passed_titles(body: Any, checked_ids: list[str]) -> frozenset[str]:
    """Normalised titles of the previous plan's passed cases, for quarantine."""
    checked = set(checked_ids)
    return frozenset(
        _norm(meta.get("title") or "")
        for case_id, meta in progress_key_service.case_index(body).items()
        if case_id in checked and meta.get("title")
    )


def render_previous_plan(
    *, plan_id: int, progress_key: str, body: Any, checked_ids: list[str]
) -> str:
    """The prompt block that lets a regeneration keep each case's id."""
    checked = set(checked_ids)
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"PREVIOUS VERSION OF THIS PLAN (plan {plan_id}, progress key {progress_key})",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "QA records results against case ids of the form <section>:<index>, counted",
        "per section in order. Moving, dropping or re-sectioning a case silently",
        "attaches recorded results to a different case. So:",
        "- Keep each case that still holds in the same section and order, and reword",
        "  it in place rather than replacing it with a new case.",
        "- Re-check every case against rules G–K and the code at the commit under",
        "  test. This version's reasons are not evidence (rule I); fix a wrong",
        "  expected result rather than keeping it.",
        "- Remove a case only when it cannot stay, e.g. a follow-up under rule J.",
        "  Its finding then MUST appear in risks_and_gaps (follow_up, owner,",
        "  suggested_ticket). Removing a case never removes the finding.",
        "- When a case's expectation was wrong, rewrite that case at its id. Do not",
        "  delete it and add the corrected version somewhere else.",
        "- Keep existing cases' priorities and order unless a priority is wrong. Add",
        "  new cases after the existing ones of the same priority.",
        "- [PASSED] means QA recorded a pass. Change what such a case checks only",
        "  if it is wrong, and then say so in its expected result.",
        "",
    ]
    for case_id, meta, item in progress_key_service.cases_with_items(body):
        mark = " [PASSED]" if case_id in checked else ""
        if isinstance(item, dict):
            lines.append(f"{case_id}{mark} — {meta.get('title') or ''}")
            if item.get("expected"):
                lines.append(f"    expected: {_short(item['expected'])}")
        else:
            lines.append(f"{case_id}{mark} — {_short(str(item), 400)}")
    return "\n".join(lines) + "\n"


def _dice(left: str, right: str) -> float:
    a, b = set(left.split()), set(right.split())
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def _score(left: str, right: str) -> float:
    if left and left == right:
        return 1.0
    return max(_similarity(left, right), _dice(left, right))


#: Lists beside the numbered sections a regenerated case can land in, with the
#: remap status that names each. ``case_disposition`` fills the last three.
_OUT_OF_SECTION = (("needs_spec_cases", "needs_spec"), ("code_review_checks", "code_review"),
                   ("pre_existing_behaviour", "pre_existing"), ("regression_low_confidence", "low_confidence"))
MOVED_OUT_STATUSES = tuple(status for _, status in _OUT_OF_SECTION)


def _needs_spec_titles(body: Any) -> list[str]:
    return [t for t, status in _out_of_section_titles(body) if status == "needs_spec"]


def _out_of_section_titles(body: Any) -> list[tuple[str, str]]:
    plan = progress_key_service._as_plan_dict(body)
    out = []
    for key, status in _OUT_OF_SECTION:
        for c in plan.get(key) or []:
            if isinstance(c, dict) and (c.get("title") or c.get("line")):
                out.append((c.get("title") or c.get("line"), status))
    return out


def map_case_ids(old_body: Any, new_body: Any) -> list[dict]:
    """Every old case id, with the new id it most likely became.

    Matched on wording, best pair first across the whole plan, so one weak
    early match cannot take a case that a later one fits exactly. A pair below
    ``MATCH_THRESHOLD`` is not a match; one below ``CONFIDENT`` carries
    ``confident: False``. The statuses:

    * ``kept`` — same id, same wording;
    * ``reworded`` — same id, different wording;
    * ``moved`` — a different id, same or similar wording;
    * ``needs_spec`` — no numbered case matches, but a quarantined one does: the
      model kept the case and the pipeline moved it out (``quarantine``);
    * ``gone`` — nothing in the new plan is close enough.
    """
    old = progress_key_service.case_index(old_body)
    new = progress_key_service.case_index(new_body)
    old_text = {i: _norm(m.get("title") or "") for i, m in old.items()}
    new_text = {i: _norm(m.get("title") or "") for i, m in new.items()}

    pairs = []
    for oi, ot in old_text.items():
        for ni, nt in new_text.items():
            score = _score(ot, nt)
            if score >= MATCH_THRESHOLD:
                # Ties go to the same id, then the same section.
                pairs.append((score, oi == ni, old[oi]["section"] == new[ni]["section"], oi, ni))
    pairs.sort(reverse=True)
    matched: dict[str, tuple[str, float]] = {}
    taken: set[str] = set()
    for score, _same_id, _same_section, oi, ni in pairs:
        if oi in matched or ni in taken:
            continue
        matched[oi] = (ni, score)
        taken.add(ni)

    moved_out = [(t, _norm(t), status) for t, status in _out_of_section_titles(new_body)]
    mapping = []
    for oi, meta in old.items():
        if oi not in matched:
            best = max(((_score(old_text[oi], qt), t, st) for t, qt, st in moved_out),
                       default=(0.0, None, None))
            if best[0] >= MATCH_THRESHOLD:
                mapping.append({"from": oi, "to": None, "status": best[2],
                                "from_title": meta.get("title"), "to_title": best[1],
                                "similarity": round(best[0], 3), "confident": best[0] >= CONFIDENT})
                continue
            mapping.append({"from": oi, "to": None, "status": "gone",
                            "from_title": meta.get("title"), "to_title": None, "similarity": 0.0})
            continue
        ni, score = matched[oi]
        if ni != oi:
            status = "moved"
        elif score >= 0.999:
            status = "kept"
        else:
            status = "reworded"
        mapping.append({"from": oi, "to": ni, "status": status,
                        "from_title": meta.get("title"), "to_title": new[ni].get("title"),
                        "similarity": round(score, 3), "confident": score >= CONFIDENT})
    return mapping


def build_progress_remap(
    *,
    ticket_keys: list[str],
    old_plan_id: int,
    old_body: Any,
    new_body: Any,
    checked_ids: list[str],
) -> dict:
    """What a regeneration does to the recorded progress, before anyone posts it.

    ``needs_carryover`` lists the recorded marks that will not be read on the
    new plan as they stand (the key changed), and ``misattached`` lists the
    ones that WILL be read but against a different case (the key is the same and
    the id now names another case). ``misattached`` is the silent one, and the
    one to fix before posting.
    """
    old_key = progress_key_service.build_progress_key(ticket_keys, old_body)
    new_key = progress_key_service.build_progress_key(ticket_keys, new_body)
    mapping = map_case_ids(old_body, new_body)
    by_from = {m["from"]: m for m in mapping}
    checked = [i for i in checked_ids if isinstance(i, str)]
    key_changed = old_key.upper() != new_key.upper()
    new_ids = set(progress_key_service.case_index(new_body))

    checked_report = []
    for cid in sorted(checked):
        m = by_from.get(cid) or {"from": cid, "to": None, "status": "gone"}
        checked_report.append({"from": cid, "to": m["to"], "status": m["status"],
                               "confident": m.get("confident", False)})
    misattached = []
    if not key_changed:
        for cid in sorted(checked):
            m = by_from.get(cid)
            if cid in new_ids and (m is None or m["to"] != cid):
                misattached.append({"id": cid, "now_names": new_body_title(new_body, cid),
                                    "was": (m or {}).get("from_title"), "belongs_at": (m or {}).get("to")})
    return {
        "previous_plan_id": old_plan_id,
        "old_key": old_key,
        "new_key": new_key,
        "key_changed": key_changed,
        "ids_changed": any(m["status"] in ("moved", "gone") + MOVED_OUT_STATUSES for m in mapping)
        or len(new_ids) != len(mapping),
        "mapping": mapping,
        "new_ids": sorted(new_ids - {m["to"] for m in mapping if m["to"]}),
        "checked": checked_report,
        "needs_carryover": [c for c in checked_report if key_changed and c["to"]],
        "misattached": misattached,
    }


def new_body_title(body: Any, case_id: str) -> str | None:
    return (progress_key_service.case_index(body).get(case_id) or {}).get("title")
