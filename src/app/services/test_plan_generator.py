"""Domain helpers for the /generate-test-plan pipeline.

Extracted from ``src/app/main.py`` so the route module stays a thin
FastAPI wrapper. Everything here is pure orchestration or
transformation: context-flag derivation, AC-coverage computation,
warning normalization, and the three post-generation critics
(grounding, code-grounding, fix-scope).

The critics catch three distinct failure modes of the LLM's first pass:
    * ``_run_grounding_critic`` — case cites an AC whose text doesn't
      describe the behaviour the case tests (LLM paraphrased or
      hallucinated the citation).
    * ``_run_code_grounding_critic`` — grounding warning is still
      pointing at behaviour that IS in the merged code; downgrades WARN
      to INFO so QA doesn't chase a false-positive.
    * ``_run_fix_scope_critic`` — case tests something the merged PR
      explicitly did NOT change (reporter drift from the ticket body).

Each critic degrades gracefully on failure so the plan still ships.
"""
import json
import logging
import re

from ..code_grounding_critic import (
    apply_code_verdicts,
    build_code_verification_inputs,
    build_search_query,
    extract_repos,
    select_recheckable_warnings,
)
from ..config import settings
from ..description_analyzer import extract_ac_action_facets
from ..fix_scope_critic import (
    apply_scope_verdicts,
    build_case_scope_inputs,
    build_fix_scope_summary,
)
from ..grounding_critic import (
    apply_verdicts,
    build_ac_index,
    build_case_verification_inputs,
)
from ..models import GenerateTestPlanRequest, TicketInput

logger = logging.getLogger(__name__)

_CROSS_PROJECT_AC_ID_RE = re.compile(r"^CROSS-\d+$")


def derive_context_flags(request: GenerateTestPlanRequest | TicketInput) -> dict:
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


def normalize_grounding_warnings(
    test_plan, valid_ac_ids: set[str] | None = None
) -> list[dict]:
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
        if (
            valid_ac_ids is not None
            and ac_id not in valid_ac_ids
            and not _CROSS_PROJECT_AC_ID_RE.match(ac_id)
        ):
            continue
        dedupe_key = (ac_id, missing.lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized: dict = {
            "ac_id": ac_id,
            "missing_element": missing,
            "explanation": explanation,
        }
        # Preserve provenance + severity + code-evidence when the critic
        # pipeline attached them. Older LLM-native warnings still land
        # here without these fields; the frontend defaults them.
        source = entry.get("source")
        if isinstance(source, str) and source.strip():
            normalized["source"] = source.strip()
        severity = entry.get("severity")
        if severity in ("warn", "info"):
            normalized["severity"] = severity
        code_evidence = entry.get("code_evidence")
        if isinstance(code_evidence, dict) and code_evidence:
            normalized["code_evidence"] = code_evidence
        out.append(normalized)
    return out


def _facet_stem(word: str) -> str:
    """Crude morphological stem so "deleted"/"delete"/"deletion" collapse to a
    common root for facet matching. Not linguistically perfect — good enough to
    match an AC's enumerated action verb against the wording of a test case."""
    w = re.sub(r"[^a-z]", "", word.lower())
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            break
    # Collapse a doubled final consonant ("resett" → "reset") so the stem of an
    # inflected form lines up with the bare verb.
    if len(w) >= 4 and w[-1] == w[-2]:
        w = w[:-1]
    return w


# Facets whose verbs are commonly expressed with a different root in a test
# step than in the AC ("deleted" vs "removed", "sent" vs "send/email").
_FACET_SYNONYM_STEMS: dict[str, set[str]] = {
    "delet": {"delet", "remov", "destroy"},
    "remov": {"remov", "delet"},
    "sent": {"sent", "send", "email"},
    "send": {"send", "sent", "email"},
    "shar": {"shar", "send", "sent"},
}


def _case_text_blob(case: dict) -> str:
    """Flatten every string value in a test case (title, steps, expected,
    test_data, preconditions, …) into one lowercase blob for keyword matching."""
    parts: list[str] = []

    def _walk(v):
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict):
            for vv in v.values():
                _walk(vv)
        elif isinstance(v, (list, tuple)):
            for vv in v:
                _walk(vv)

    # Don't recurse into covers_acs / grounded_in — those are IDs, not behaviour.
    for key, value in case.items():
        if key in ("covers_acs", "grounded_in"):
            continue
        _walk(value)
    return " ".join(parts).lower()


def _facet_is_covered(facet: str, text_stems: set[str]) -> bool:
    """True if the AC action `facet` is mentioned anywhere in the covering
    cases' text (matched on stems, with a small synonym set)."""
    fstem = _facet_stem(facet)
    if len(fstem) < 3:
        # Too short to match reliably; treat as covered to avoid noise.
        return True
    candidates = _FACET_SYNONYM_STEMS.get(fstem, {fstem})
    for cand in candidates:
        for ts in text_stems:
            if ts == cand or ts.startswith(cand) or cand.startswith(ts):
                return True
    return False


def compute_ac_coverage(test_plan, tickets_data: list[dict]) -> dict:
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
        from ..llm_client import _ticket_key_recency
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
    # ac_id → list of case-text blobs, so a compound AC can be checked PER
    # enumerated action against the cases that actually claim to cover it.
    cases_by_ac: dict[str, list[str]] = {}
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
                    cases_by_ac.setdefault(trimmed, []).append(_case_text_blob(case))
                else:
                    invalid_ids.add(trimmed)
            # Rewrite the case so the UI / persisted plan only show real IDs.
            case["covers_acs"] = kept

    result_tickets: dict[str, dict] = {}
    uncovered_total = 0
    under_covered_total = 0
    winner_by_loser = {p["loser_id"]: p["winner_id"] for p in superseded_pairs}
    for key, entries in per_ticket.items():
        covered: list[str] = []
        uncovered: list[dict] = []
        superseded: list[dict] = []
        under_covered: list[dict] = []
        for ac_id, text in entries:
            if ac_id in superseded_loser_ids:
                superseded.append({
                    "id": ac_id,
                    "text": text,
                    "winner_id": winner_by_loser[ac_id],
                })
            elif ac_id in declared:
                covered.append(ac_id)
                # The AC ID is tagged, but if the AC enumerates multiple
                # discrete actions (e.g. "created, updated, or deleted"),
                # confirm EACH action is actually exercised by some covering
                # case. A subset (only created+updated) ships the rest
                # untested while the bullet-level check reads as fully covered.
                facets = extract_ac_action_facets(text)
                if facets:
                    blob = " ".join(cases_by_ac.get(ac_id, []))
                    text_stems = {_facet_stem(w) for w in re.findall(r"[a-z]+", blob)}
                    missing = [f for f in facets if not _facet_is_covered(f, text_stems)]
                    if missing:
                        under_covered.append({
                            "id": ac_id,
                            "text": text,
                            "missing_actions": missing,
                            "actions": facets,
                        })
            else:
                uncovered.append({"id": ac_id, "text": text})
        uncovered_total += len(uncovered)
        under_covered_total += len(under_covered)
        result_tickets[key] = {
            "covered": covered,
            "uncovered": uncovered,
            "superseded": superseded,
            # ACs whose ID is tagged but whose enumerated sub-actions are not
            # all exercised. They count as "covered" for the X/Y ratio but are
            # surfaced so QA can see the partial coverage and push back.
            "under_covered": under_covered,
            # `total` excludes superseded ACs so the X/Y ratio in the UI
            # reflects what was actually expected to be tested.
            "total": len(entries) - len(superseded),
        }
    return {
        "tickets": result_tickets,
        "uncovered_total": uncovered_total,
        "under_covered_total": under_covered_total,
        "invalid_ids": sorted(invalid_ids),
        "superseded_acs": superseded_pairs,
    }


async def run_grounding_critic(llm, test_plan, tickets_data: list[dict]) -> None:
    """Post-generation critic — check that each case's cited AC actually
    describes the behaviour the case tests.

    Any case flagged ungrounded is badged in place with
    ``needs_manual_verification=True`` and gets an entry in the plan's
    ``grounding_warnings`` list. The UI then renders the existing
    "Unverified UI" badge on the case so QA can visibly skip it.

    Failures inside the critic (transport errors, malformed output,
    provider not implementing it) are non-fatal — the plan ships without
    the extra badges rather than with a broken request.
    """
    ac_index = build_ac_index(tickets_data)
    if not ac_index:
        return
    cases = build_case_verification_inputs(test_plan, ac_index)
    if not cases:
        return
    try:
        verdicts = await llm.verify_case_grounding(cases)
    except Exception:
        logger.exception("verify_case_grounding raised; skipping grounding critic")
        return
    added = apply_verdicts(test_plan, verdicts)
    if added:
        logger.info(
            "grounding_critic: flagged %d ungrounded case(s): %s",
            len(added),
            [w["ac_id"] + ": " + w["missing_element"] for w in added],
        )


async def run_code_grounding_critic(
    llm,
    test_plan,
    dev_infos: list[dict],
) -> None:
    """Third-pass critic — for each AC-grounding warning the previous
    critic added, look at the linked repo's actual source and downgrade
    the warning to informational when the behaviour under test is
    demonstrably implemented in code.

    Runs only when a GitHub token is configured, the feature toggle is
    on, at least one linked PR carries a resolved ``repository``, and
    ``grounding_critic`` produced at least one ``critic_ac`` warning.
    Any failure — no repo, code-search miss, LLM error — degrades to
    leaving the warning at WARN severity, so QA sees the same output
    they saw before this pass existed.
    """
    if not settings.code_grounding_recheck_enabled:
        return
    if not settings.github_token:
        return

    warnings = list(getattr(test_plan, "grounding_warnings", None) or [])
    recheckable = select_recheckable_warnings(warnings)
    if not recheckable:
        return

    repos = extract_repos(dev_infos)
    if not repos:
        return

    from ..github_client import GitHubClient
    client = GitHubClient()

    hits_by_warning: dict[int, list[dict]] = {}
    for i, warning in enumerate(recheckable):
        query = build_search_query(warning)
        if not query:
            continue
        # Search each linked repo in order; stop as soon as we get
        # anything. Most tickets link one repo — the second-repo path
        # exists only for cross-project batches.
        for repo in repos:
            try:
                hits = await client.search_relevant_files(repo, query, max_files=3)
            except Exception:
                logger.exception(
                    "code_grounding_critic: search failed repo=%s q=%r", repo, query
                )
                hits = []
            if hits:
                hits_by_warning[i] = hits
                break

    cases = build_code_verification_inputs(test_plan, recheckable, hits_by_warning)
    if not cases:
        return

    try:
        verdicts = await llm.verify_code_grounding(cases)
    except Exception:
        logger.exception(
            "verify_code_grounding raised; skipping code-grounding critic"
        )
        return

    evidence_by_key = {c["warning_key"]: c["code_snippets"] for c in cases}
    downgraded = apply_code_verdicts(test_plan, verdicts, evidence_by_key)
    if downgraded:
        logger.info(
            "code_grounding_critic: downgraded %d warning(s) after code recheck: %s",
            len(downgraded),
            [w["ac_id"] + ": " + w["missing_element"] for w in downgraded],
        )


async def run_fix_scope_critic(
    llm,
    test_plan,
    dev_infos: list[dict],
) -> None:
    """Post-generation critic — check that each case exercises behaviour the
    merged PR actually changed.

    Catches reporter-drift: cases that cite a real AC but test a concern
    from the ticket description that the fix explicitly did NOT address
    (e.g. SK-2373 EC-0 asserting the FRED default rate is not auto-applied
    when the merged PR body says "Tooltip copy only, no change to FRED
    behavior"). Unsupported cases are badged in place with
    ``needs_manual_verification=True`` and gain a matching
    ``grounding_warnings`` entry.

    Runs only when at least one ticket has PR/commit signal — with no fix
    context, the critic has nothing to reason against. Failures inside the
    critic (transport errors, malformed output, provider not implementing
    it) are non-fatal.
    """
    fix_scope = build_fix_scope_summary(dev_infos)
    if not fix_scope:
        return
    cases = build_case_scope_inputs(test_plan)
    if not cases:
        return
    try:
        verdicts = await llm.verify_fix_scope(cases, fix_scope)
    except Exception:
        logger.exception("verify_fix_scope raised; skipping fix-scope critic")
        return
    added = apply_scope_verdicts(test_plan, verdicts)
    if added:
        logger.info(
            "fix_scope_critic: flagged %d unsupported case(s): %s",
            len(added),
            [w["ac_id"] + ": " + w["missing_element"] for w in added],
        )


def flatten_cases_for_persistence(test_plan) -> list[tuple[str, str, str | None]]:
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
            category = "integration:cross_project" if item.get("cross_project") else "integration"
            cases.append((item.get("title", ""), _structured_case_body(item), category))
    for item in test_plan.regression_checklist or []:
        if isinstance(item, str):
            cases.append((item, "", "regression"))

    return cases
