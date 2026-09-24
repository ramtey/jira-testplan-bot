"""Adopt a plan that exists only as a Jira comment back into the database.

Why this exists
---------------
A plan can reach Jira without ever being persisted. ``run_tracker.start_run``
returns ``RunContext(run_id=None)`` when the database is unreachable, so
``complete_with_plan`` short-circuits to ``None``, ``generate_single`` omits
``plan_id`` from its response, and ``/jira/post-comment`` accepts ``plan_id=None``
and posts anyway. The plan lands on the ticket and nothing records it.

SK-2342 is the case this was written for: a transient Atlas outage on 2026-09-15
killed four watcher sweeps outright, and the plan that eventually reached the
ticket (comment 330141) left no run, no plan and no ``jira_tickets`` snapshot.
With neither record there is no ``build_progress_key(run.ticket_keys, plan.body)``
to write UAT progress under and none for the UI to poll.

Regenerating is not a fix: the LLM is non-deterministic, so a new plan would
differ from the one a human already reviewed on the ticket, and the runner would
be marking cases nobody signed off on. This module reconstructs the *existing*
plan from its own Jira comment instead.

What survives the round trip
----------------------------
The plan is posted as ADF built by ``jira_client``: each case becomes a
``nestedExpand`` whose ``attrs.title`` is the rendered ``N. Title 🔴 PRIORITY
[category]`` line, with preconditions/steps/expected/test-data as its content.
Parsing that structure back is the inverse of
``_group_test_cases_into_nested_expands`` and recovers everything the manual
checklist and the progress key depend on.

What does not survive is the machine-only metadata the renderer never wrote:
``covers_acs``, ``grounded_in``, ``expected_source`` and ``ac_coverage``. Adopted
plans are therefore marked ``adopted_from_jira`` so nobody mistakes a
reconstruction for a generated plan.

Why the fingerprint is still exact
----------------------------------
``formatTestPlanAsJira`` renders the four graded sections through ``uncovered()``,
so cases flagged ``covered_by_unit_test`` are never among the numbered cases.
``progress_key.fingerprint`` counts those same sections with the same cases
filtered out, so counting what the comment contains reproduces the first four
numbers by construction.

The fifth number needs more than that. Covered cases are addressable now
(``covered_by_unit_test:<n>``) and their count is part of the key, so they cannot
simply be dropped on the way back in — a plan adopted without them derives a
different key from the plan it was posted from, which is the SK-2642 split
reappearing on the recovery path. The comment therefore always carries them, in
a trailing list that names the section each was lifted from, and
``_parse_covered`` files them back there.

Every case also prints its canonical ``[section:index]`` id, so the parser reads
the section a case belongs to rather than inferring it from the last banner it
saw. That is strictly better evidence: a part boundary that loses a banner used
to drop everything after it.

Single producer is preserved: this module produces a plan *body* and nothing
else. The key is still derived only by ``progress_key.build_progress_key`` from
that stored body, the way ``/plans/{id}/progress-key`` already does it. Nothing
here counts a section into a key.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.app.services import progress_key as progress_key_service

# The graded case sections, in the order ``progress_key.CHECKLIST_KEYS`` fixes.
# Matched on the full heading text rather than the leading emoji: "🔗 GROUNDED
# IN" shares its prefix with "🔗 INTEGRATION & BACKEND TESTS" and is not a
# section.
_SECTION_HEADINGS: tuple[tuple[str, str], ...] = (
    ("✅ HAPPY PATH TEST CASES", "happy_path"),
    ("🔍 EDGE CASES & ERROR SCENARIOS", "edge_cases"),
    ("🔗 INTEGRATION & BACKEND TESTS", "integration_tests"),
    ("🔐 SECURITY NEGATIVE TESTS", "security_negative_tests"),
)

_REGRESSION_HEADING = "🔄 REGRESSION CHECKLIST"

# The sections a printed case id may name. Anything else in that slot is a
# malformed id, and the section the walk is standing in is the safer reading.
_CASE_SECTIONS = frozenset(key for _heading, key in _SECTION_HEADINGS)

# Headings that end the regression checklist. The ADF builder's
# ``_collect_until_next_section`` stops only at ``_SECTION_PREFIXES``
# ('✅', '🔍', '🔗', '🔄'), so everything after the regression banner —
# risks/gaps, needs-spec, the covered-case list — is swallowed into that one
# nestedExpand. The bullets in those trailing sections look exactly like
# regression bullets, so the parser has to stop where the builder didn't.
_REGRESSION_TERMINATORS = (
    "⚠️ RISKS / GAPS OBSERVED",
    "🚧 NEEDS SPEC",
    "🧪 ALREADY COVERED BY UNIT TESTS",
)

_CASE_TITLE_RE = re.compile(r"^\s*(\d+)\.\s+(.*)$", re.DOTALL)
# Leading "[edge_cases:3]" — the canonical id the renderer now prints beside
# every case so a reader never has to map the displayed grouping onto storage.
# Parsed back rather than merely stripped: it names the section the case belongs
# to outright, which is stronger than inferring it from the last banner seen.
_CASE_ID_RE = re.compile(r"^\[([a-z_]+):(\d+)\]\s*")
# "[covered_by_unit_test:0] Title (from integration_tests; covered by src/x.test.ts)"
_COVERED_CASE_RE = re.compile(
    r"^\[covered_by_unit_test:(\d+)\]\s*(.*?)"
    r"\s*\(from (happy_path|edge_cases|integration_tests|security_negative_tests)"
    r"(?:;\s*covered by\s*(.+?))?\)$"
)
_COVERED_HEADING = "🧪 ALREADY COVERED BY UNIT TESTS"
# Trailing "🔴 CRITICAL" / "🟡 HIGH" / "🟢 MEDIUM" appended by the renderer.
_PRIORITY_RE = re.compile(r"\s*[🔴🟡🟢]\s*(CRITICAL|HIGH|MEDIUM|LOW)\s*$")
# Trailing "[error_handling]" appended for edge cases.
_CATEGORY_RE = re.compile(r"\s*\[([^\]]+)\]\s*$")

_BULLET_PREFIXES = ("•", "●", "-", "*")
# "🤖 Generated Test Plan (part 2 of 3)" — the header `jira_client` puts on each
# comment when a plan is too large to post as one.
_PART_HEADER_RE = re.compile(r"\(part\s+(\d+)\s+of\s+(\d+)\)")


class PlanAdoptionError(Exception):
    """The comment could not be read as a test plan."""


class IncompletePlanParts(PlanAdoptionError):
    """A split plan declares M parts and the ticket carries fewer.

    Carries the parts that *are* present, because preview and commit owe the
    caller different things here. ``preview`` writes nothing, and its whole job
    is to make a misparse visible before it strands progress — so it reports the
    short counts with this as a warning, and the operator can see which parts
    the ticket is missing. ``commit`` lets it propagate: a plan adopted with a
    hole derives a key for a fraction of the plan, which is the failure this
    module exists to end.
    """

    def __init__(self, message: str, parts: list[dict]):
        super().__init__(message)
        self.parts = parts


def _text(node: dict) -> str:
    if node.get("type") == "text":
        return node.get("text") or ""
    return "".join(_text(child) for child in node.get("content") or [])


def _para_text(node: dict) -> str:
    return _text(node).strip()


def _strip_bullet(text: str) -> str:
    stripped = text.strip()
    for prefix in _BULLET_PREFIXES:
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    return stripped


def _find_plan_container(adf: dict) -> list[dict]:
    """The nodes holding the plan body.

    Normally the whole plan sits inside the single ``expand`` that
    ``_wrap_body_in_expand`` adds. Comments posted before that wrapper existed
    have the nodes at the document's top level, so fall back to that.
    """
    content = adf.get("content") or []
    for node in content:
        if node.get("type") == "expand":
            return node.get("content") or []
    return content


def _split_title(raw: str) -> tuple[dict, str | None]:
    """Pull ``N. [section:index] Title 🔴 PRIORITY [category]`` apart.

    Returns the case and the section its printed id names, or ``None`` when the
    comment predates ids. The id is stripped from the title either way — it is
    an address, not part of what the case says.
    """
    match = _CASE_TITLE_RE.match(raw.strip())
    remainder = match.group(2).strip() if match else raw.strip()

    id_section: str | None = None
    id_match = _CASE_ID_RE.match(remainder)
    if id_match:
        id_section = id_match.group(1)
        remainder = remainder[id_match.end():].strip()

    priority: str | None = None
    category: str | None = None

    cat_match = _CATEGORY_RE.search(remainder)
    if cat_match:
        category = cat_match.group(1).strip()
        remainder = remainder[: cat_match.start()].strip()

    pri_match = _PRIORITY_RE.search(remainder)
    if pri_match:
        priority = pri_match.group(1).lower()
        remainder = remainder[: pri_match.start()].strip()

    case: dict[str, Any] = {"title": remainder}
    if priority:
        case["priority"] = priority
    if category:
        # The same trailing bracket token carries two different fields
        # depending on the section it was printed in: `edge_cases` prints its
        # `category`, the security section prints its `security_category`.
        # Filing one under the other's name would put "client_flag" in the
        # edge-category chip and leave the security chip blank.
        if id_section == "security_negative_tests":
            case["security_category"] = category
        else:
            case["category"] = category
    return case, id_section


def _parse_case_details(nodes: list[dict], case: dict) -> None:
    """Fill preconditions/steps/expected/test_data from a case's ADF content."""
    steps: list[str] = []
    for node in nodes:
        node_type = node.get("type")
        if node_type in ("orderedList", "bulletList"):
            for item in node.get("content") or []:
                step = _para_text(item)
                if step:
                    steps.append(step)
            continue
        if node_type not in ("paragraph", "heading"):
            continue
        text = _para_text(node)
        if not text:
            continue
        if text.startswith("Preconditions:"):
            case["preconditions"] = text[len("Preconditions:"):].strip()
        elif text.startswith("Expected Result:"):
            case["expected"] = text[len("Expected Result:"):].strip()
        elif text.startswith("Test Data:"):
            case["test_data"] = text[len("Test Data:"):].strip()
        elif text.startswith("Runs on:"):
            # "Runs on: Mobile · credentials: … · environment: …" — kept whole
            # rather than split, because the separator also appears inside the
            # free-text credential and environment values.
            case["surface_line"] = text[len("Runs on:"):].strip()
        elif text.startswith("⚠️ Needs manual verification"):
            case["needs_manual_verification"] = True
        elif text.startswith("⚠️ Citation unconfirmed"):
            # Restored so an adopted plan carries the same caveat the tester
            # read. The reason text is the renderer's, not reconstructable, so
            # keep it verbatim.
            case["expected_source_unconfirmed"] = True
            case["expected_source_unconfirmed_reason"] = text.split("—", 1)[-1].strip()
        elif text.startswith("⚠️ Unverified"):
            # The renderer emits this footer only for expected_verified=False.
            case["expected_verified"] = False
    if steps:
        case["steps"] = steps


def _parse_regression(nodes: list[dict]) -> tuple[list[str], list[dict]]:
    """Leading bullets of the regression nestedExpand, up to the next section.

    Also returns the nodes from that next section onward. The ADF builder's
    ``_collect_until_next_section`` stops only at the four section emoji, so
    risks/gaps, needs-spec *and the covered-case list* are all swallowed into
    this one nestedExpand. Handing the tail back is what lets the covered list
    be read rather than dropped — and a dropped covered list is a plan whose
    fingerprint no longer matches the one it was posted from.
    """
    items: list[str] = []
    for position, node in enumerate(nodes):
        if node.get("type") in ("bulletList", "orderedList"):
            for item in node.get("content") or []:
                text = _strip_bullet(_para_text(item))
                if text:
                    items.append(_strip_case_id(text))
            continue
        if node.get("type") not in ("paragraph", "heading"):
            continue
        text = _para_text(node)
        if not text:
            continue
        if text.startswith(_REGRESSION_TERMINATORS):
            return items, list(nodes[position:])
        items.append(_strip_case_id(_strip_bullet(text)))
    return items, []


def _strip_case_id(text: str) -> str:
    """Drop a leading ``[regression_checklist:2]`` address from a bullet."""
    return _CASE_ID_RE.sub("", text, count=1).strip()


def _covered_lines(nodes: list[dict]) -> list[str]:
    """Every bullet line in `nodes`, flattened — paragraphs and lists alike."""
    lines: list[str] = []
    for node in nodes:
        node_type = node.get("type")
        if node_type in ("bulletList", "orderedList"):
            for item in node.get("content") or []:
                text = _strip_bullet(_para_text(item))
                if text:
                    lines.append(text)
            continue
        if node_type not in ("paragraph", "heading"):
            continue
        text = _para_text(node)
        if text:
            lines.append(_strip_bullet(text))
    return lines


def _parse_covered(nodes: list[dict]) -> list[tuple[str, dict]]:
    """Cases the comment lists as already covered by a unit test.

    Returns ``(origin_section, case)`` pairs in printed order. The origin
    section is printed precisely so this can put each case back where it came
    from: covered cases do not count towards their own section's size, but they
    do count towards the fingerprint's fifth component, so losing them here
    would make an adopted plan derive a different key from the plan it was
    rendered out of — the SK-2642 split, from the recovery path.
    """
    out: list[tuple[str, dict]] = []
    started = False
    for line in _covered_lines(nodes):
        if line.startswith(_COVERED_HEADING):
            started = True
            continue
        if not started:
            continue
        match = _COVERED_CASE_RE.match(line)
        if not match:
            continue
        _index, title, section, ref = match.groups()
        case: dict[str, Any] = {
            "title": title.strip(),
            "covered_by_unit_test": True,
        }
        if ref:
            case["unit_test_ref"] = ref.strip()
        out.append((section, case))
    return out


def _section_for_heading(text: str) -> str | None:
    for heading, key in _SECTION_HEADINGS:
        if text.startswith(heading):
            return key
    return None


def parse_plan_from_adf(adf: dict, *, ticket_key: str) -> dict:
    """Reconstruct a plan body from the ADF of its Jira comment."""
    return parse_plan_from_parts([adf], ticket_key=ticket_key)


def parse_plan_from_parts(adfs: list[dict], *, ticket_key: str) -> dict:
    """Reconstruct a plan body from the ADF of every comment it was posted as.

    Returns a dict shaped like a stored ``json`` plan body — the same shape
    ``progress_key.fingerprint`` and the frontend's ``displayPlan`` read.

    Parts are walked as one continuous document: the open section carries
    across the boundary, so a part that starts mid-section — its banner
    repeated as "… (continued)" — files its cases under the right key instead
    of being dropped as headless.
    """
    part_nodes = [_find_plan_container(adf) for adf in adfs]
    if not any(part_nodes):
        raise PlanAdoptionError("Comment has no readable content")

    plan: dict[str, Any] = {
        "ticket_key": ticket_key.upper(),
        "happy_path": [],
        "edge_cases": [],
        "integration_tests": [],
        "security_negative_tests": [],
        "regression_checklist": [],
        "adopted_from_jira": True,
    }
    warnings: list[str] = []
    current: str | None = None
    # Covered cases are printed as one trailing list rather than inside their
    # sections, so they are gathered separately and filed back at the end.
    covered: list[tuple[str, dict]] = []
    # The marker paragraph sits outside the expand that holds the body, so it
    # has to be looked for across the whole document rather than in `nodes`.
    saw_marker = any("🤖" in _text(adf)[:4000] for adf in adfs)

    for node in [node for nodes in part_nodes for node in nodes]:
        node_type = node.get("type")

        if node_type in ("paragraph", "heading"):
            text = _para_text(node)
            if not text:
                continue
            if text.startswith("UAT complexity:"):
                plan["uat_complexity"] = text[len("UAT complexity:"):].strip().lower()
                continue
            section = _section_for_heading(text)
            if section is not None:
                current = section
                continue
            # A `**N. Title**` paragraph: a case in a comment whose cases were
            # never grouped into nestedExpands.
            if _CASE_TITLE_RE.match(text):
                case, id_section = _split_title(text)
                target = id_section if id_section in _CASE_SECTIONS else current
                if target:
                    plan[target].append(case)
            continue

        if node_type != "nestedExpand":
            continue

        title = (node.get("attrs") or {}).get("title") or ""
        if title.startswith(_REGRESSION_HEADING):
            # Extend rather than assign: a checklist long enough to be split
            # across parts appears once per part, and assigning would keep only
            # the last part's bullets.
            items, tail = _parse_regression(node.get("content") or [])
            plan["regression_checklist"].extend(items)
            covered.extend(_parse_covered(tail))
            current = None
            continue
        if not _CASE_TITLE_RE.match(title):
            continue
        case, id_section = _split_title(title)
        target = id_section if id_section in _CASE_SECTIONS else current
        if target is None:
            warnings.append(
                f"Case {title[:60]!r} appeared before any section heading and was skipped"
            )
            continue
        _parse_case_details(node.get("content") or [], case)
        plan[target].append(case)

    # Nothing above found the covered list — it can also sit at the top level in
    # a comment the ADF builder never grouped.
    if not covered:
        covered = _parse_covered([n for nodes in part_nodes for n in nodes])
    for section, case in covered:
        plan[section].append(case)

    if not saw_marker:
        warnings.append(
            "Comment carries no '🤖 Generated Test Plan' marker — it may not be a bot plan"
        )
    if not any(plan.get(key) for key in progress_key_service.SECTION_KEYS):
        raise PlanAdoptionError(
            "No test cases found in the comment — nothing to adopt"
        )

    plan["adoption_warnings"] = warnings
    return plan


def summarize(plan: dict, ticket_keys: list[str]) -> dict:
    """Counts, fingerprint and derived key for a parsed plan.

    The key comes from ``build_progress_key`` over the serialized body — the
    same call ``/plans/{id}/progress-key`` makes — so this preview cannot drift
    from what the adopted plan will actually produce.
    """
    body = json.dumps(plan)
    return {
        "section_counts": {
            key: len(plan.get(key) or []) for key in progress_key_service.SECTION_KEYS
        },
        "case_count": sum(
            len(plan.get(key) or []) for key in progress_key_service.SECTION_KEYS
        ),
        "fingerprint": progress_key_service.fingerprint(body),
        "progress_key": progress_key_service.build_progress_key(ticket_keys, body),
        "warnings": plan.get("adoption_warnings") or [],
    }


# --- Adoption ------------------------------------------------------------

# Recorded on the run so adopted plans are distinguishable from generated ones
# in analytics and in the history UI. Token counts and cost stay zero: no model
# was called, and a nonzero cost here would corrupt spend aggregations.
ADOPTED_MODEL = "adopted-from-jira"
ADOPTED_PROVIDER = "none"


def _is_plan_comment(comment: dict) -> bool:
    from src.app.jira_client import _comment_carries_test_plan_marker

    return _comment_carries_test_plan_marker(comment)


def _marker_text(comment: dict) -> str:
    """The marker paragraph of a plan comment, which carries the part header."""
    content = (comment.get("body") or {}).get("content") or []
    return _para_text(content[0]) if content else ""


def _part_of(comment: dict) -> tuple[int, int]:
    """``(index, total)`` from a ``(part N of M)`` marker; ``(1, 1)`` without one.

    A plan too large for one Jira comment is posted as several, each carrying
    the marker. Adopting only one of them reconstructs a fraction of the plan
    and derives a progress key for that fraction — a key the UI never polls.
    """
    match = _PART_HEADER_RE.search(_marker_text(comment))
    if not match:
        return (1, 1)
    return (int(match.group(1)), int(match.group(2)))


def select_comments(comments: list[dict], comment_id: str | None) -> list[dict]:
    """The comments making up one plan, in part order.

    A single-comment plan is a one-element list. A split plan returns every
    part, so the caller parses the whole plan rather than whichever part
    happened to be posted last.
    """
    plan_comments = [c for c in comments if _is_plan_comment(c)]

    if comment_id:
        named = next(
            (c for c in comments if str(c.get("id")) == str(comment_id)), None
        )
        if named is None:
            raise PlanAdoptionError(f"Comment {comment_id} not found on this ticket")
        # Naming one part of a split plan means adopting that plan, not that
        # fragment — pull in its siblings.
        _, total = _part_of(named)
        if total > 1 and _is_plan_comment(named):
            return _parts_group(plan_comments, total)
        return [named]

    if not plan_comments:
        raise PlanAdoptionError(
            "No bot-generated test plan comment found on this ticket — "
            "pass a comment_id to adopt a specific comment"
        )
    _, total = _part_of(plan_comments[-1])
    if total > 1:
        return _parts_group(plan_comments, total)
    return [plan_comments[-1]]


def _parts_group(plan_comments: list[dict], total: int) -> list[dict]:
    """The `total`-part plan among `plan_comments`, ordered part 1..N.

    Missing parts are an error rather than a quiet short plan: a plan adopted
    with a hole in it produces wrong section counts, and the wrong counts are
    exactly what strands UAT progress under an unpollable key.
    """
    group = [c for c in plan_comments if _part_of(c)[1] == total]
    group.sort(key=lambda c: _part_of(c)[0])
    found = [_part_of(c)[0] for c in group]
    missing = [n for n in range(1, total + 1) if n not in found]
    if missing:
        raise IncompletePlanParts(
            f"Test plan is split across {total} comments but "
            f"part{'s' if len(missing) > 1 else ''} "
            f"{', '.join(str(n) for n in missing)} "
            f"{'are' if len(missing) > 1 else 'is'} missing from this ticket "
            f"(found part{'s' if len(found) > 1 else ''} "
            f"{', '.join(str(n) for n in found)}) — "
            "repost the plan before adopting it",
            group,
        )
    return group


def select_comment(comments: list[dict], comment_id: str | None) -> dict:
    """The first comment of the plan to adopt. Kept for callers that want a
    single comment; use ``select_comments`` to get every part of a split plan."""
    return select_comments(comments, comment_id)[0]


async def preview(ticket_key: str, comment_id: str | None = None) -> dict:
    """Parse the comment and report what adopting it would produce. Writes nothing.

    The counts and key here come from the same parse and the same
    ``build_progress_key`` call that ``commit`` will persist, so a misparse is
    visible before it can strand UAT progress under a key the UI never polls —
    which is the failure mode ``progress_key``'s docstring exists to describe.
    """
    from src.app.jira_client import JiraClient

    key = ticket_key.upper()
    comments = await JiraClient().get_comments(key)
    # A short set is reported rather than refused: preview writes nothing, and
    # seeing the counts next to "part 2 is missing" is what tells an operator
    # which comment to repost. ``commit`` refuses the same set.
    selection_warnings: list[str] = []
    try:
        parts = select_comments(comments, comment_id)
        complete = True
    except IncompletePlanParts as exc:
        parts = exc.parts
        selection_warnings.append(str(exc))
        complete = False

    plan = parse_plan_from_parts([c.get("body") or {} for c in parts], ticket_key=key)
    summary = summarize(plan, [key])
    summary.update(
        {
            "ticket_key": key,
            "comment_id": str(parts[0].get("id")),
            "comment_ids": [str(c.get("id")) for c in parts],
            "comment_created": parts[0].get("created"),
            "committed": False,
            # False means the counts above are a fragment, and committing this
            # selection will be refused.
            "complete": complete,
            "warnings": [*selection_warnings, *(summary.get("warnings") or [])],
        }
    )
    return summary


async def commit(ticket_key: str, comment_id: str | None = None) -> dict:
    """Adopt the comment: persist a run + plan so the existing progress-key
    derivation works unchanged.

    Refuses when the ticket already has a stored plan. Adoption is a recovery
    path for a plan that was never persisted, not a way to add a second plan
    alongside a real one — and ``has_successful_test_plan`` is what the
    watcher's never-regenerate guard keys off.

    Also refuses an incomplete split: ``select_comments`` raises
    ``IncompletePlanParts`` when parts are declared but absent, and unlike
    ``preview`` this path does not catch it. Persisting a fragment writes a key
    derived from a fraction of the plan, and the ticket then has a stored plan,
    so the guard above blocks adopting it properly once the missing part is
    back.
    """
    import json as _json

    from src.app.db.models.plan import PlanFormat
    from src.app.db.models.run import RunStatus, RunType
    from src.app.db.mongo import get_db
    from src.app.jira_client import JiraClient
    from src.app.repositories import (
        jira_ticket_repository,
        plan_repository,
        run_repository,
        user_repository,
    )
    from src.app.services.run_tracker import _actor_email

    key = ticket_key.upper()
    comments = await JiraClient().get_comments(key)
    parts = select_comments(comments, comment_id)
    plan = parse_plan_from_parts([c.get("body") or {} for c in parts], ticket_key=key)

    db = get_db()
    if await plan_repository.has_successful_test_plan(db, ticket_key=key):
        raise PlanAdoptionError(
            f"{key} already has a stored plan — adoption is only for plans that "
            "were never persisted"
        )

    user = await user_repository.get_or_create_by_email(db, email=_actor_email())
    await jira_ticket_repository.upsert_snapshot(db, ticket_key=key)
    run = await run_repository.create(
        db,
        user_id=user.id,
        run_type=RunType.test_plan,
        ticket_keys=[key],
        model=ADOPTED_MODEL,
        llm_provider=ADOPTED_PROVIDER,
        status=RunStatus.ok,
    )

    body = _json.dumps(plan)
    saved = await plan_repository.save_with_cases(
        db,
        run_id=run.id,
        format=PlanFormat.json,
        body=body,
        cases=_flatten_cases(plan),
    )
    # The plan demonstrably is live on the ticket — that comment is where it
    # came from — so record it rather than leaving the UI to report it untracked.
    await plan_repository.mark_plan_posted_to_jira(
        db,
        plan_id=saved.id,
        ticket_key=key,
        jira_comment_id=str(parts[0].get("id")),
    )

    summary = summarize(plan, list(run.ticket_keys or []))
    summary.update(
        {
            "ticket_key": key,
            "comment_id": str(parts[0].get("id")),
            "comment_ids": [str(c.get("id")) for c in parts],
            "run_id": run.id,
            "plan_id": saved.id,
            "committed": True,
            # Always true — an incomplete set never reaches here — but kept so
            # the field does not vanish between preview and commit responses.
            "complete": True,
        }
    )
    return summary


def _flatten_cases(plan: dict) -> list[tuple[str, str, str | None]]:
    """Cases as ``(title, body, category)`` rows, mirroring
    ``test_plan_generator.flatten_cases_for_persistence`` so an adopted plan's
    ``plan_test_cases`` look like a generated plan's."""
    rows: list[tuple[str, str, str | None]] = []

    def _body(case: dict) -> str:
        parts = []
        if case.get("preconditions"):
            parts.append(f"Preconditions: {case['preconditions']}")
        if case.get("steps"):
            parts.append("Steps:\n" + "\n".join(f"- {s}" for s in case["steps"]))
        if case.get("expected"):
            parts.append(f"Expected: {case['expected']}")
        if case.get("test_data"):
            parts.append(f"Test data: {case['test_data']}")
        return "\n\n".join(parts)

    for case in plan.get("happy_path") or []:
        rows.append((case.get("title", ""), _body(case), "happy_path"))
    for case in plan.get("edge_cases") or []:
        rows.append(
            (case.get("title", ""), _body(case), f"edge:{case.get('category', 'edge')}")
        )
    for case in plan.get("integration_tests") or []:
        rows.append((case.get("title", ""), _body(case), "integration"))
    for case in plan.get("security_negative_tests") or []:
        rows.append(
            (
                case.get("title", ""),
                _body(case),
                f"security:{case.get('security_category', 'security')}",
            )
        )
    for item in plan.get("regression_checklist") or []:
        rows.append((str(item)[:512], str(item), "regression"))
    return rows
