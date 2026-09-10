"""Resolve which source material a run is allowed to ground a plan in.

Every generated case is only as good as the code it was written from, and
until this module existed the pipeline made no distinction between code
that shipped, code that might ship, and code that was explicitly thrown
away. ``development_info`` arrived from Jira's dev-status API (or the
text-link fallback) as a flat list of pull requests, and
``_build_prompt`` rendered all of them under the heading "The following
development work has been completed for this ticket" — a closed-unmerged
PR's full diff included.

Two production failures on 2026-09-09 came out of that:

* **SK-2563** — three cases described
  ``deriveUserCalculatorOverridesBackfillDecision`` and its tests. Those
  symbols only ever existed on the branch behind PR #1303, which was
  closed unmerged ~25 hours before the plan was generated, with a comment
  explaining that the migration had been abandoned. QA spent a cycle
  deferring cases for a script that will never run.
* **SK-2609** — no PR, no branch, no commit anywhere in the repo. The
  generator emitted ten cases of pure speculation, every one badged
  "Unverified — assumption", and dropped them into the same numbered
  sections as verified cases where they were graded like everything else.

So state is a first-class input here, not a label:

    merged           authoritative. Grounds gradeable cases.
    open             legitimate, but the code can still move. Grounds
                     gradeable cases, flagged so the plan says so.
    closed_unmerged  never grounds anything. Dropped before the prompt.
    unknown          grounds cases, flagged. Reached when there is no
                     GitHub token to confirm Jira's cached status; a PR
                     we could not check is NOT the same fact as a PR we
                     checked and found abandoned, and refusing to plan
                     for every token-less deployment would be a far
                     bigger failure than the one this module fixes.

Known limitation: dev-status commits carry no PR association, so a commit
belonging to an excluded PR still reaches the prompt as a one-line
message. Branches are attributable (via each PR's ``source_branch``) and
are dropped; commits are not, and guessing an attribution would be worse
than the one-line leak.

Everything here is pure — no network, no DB — so the policy can be tested
without either.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

PR_STATE_MERGED = "merged"
PR_STATE_OPEN = "open"
PR_STATE_CLOSED_UNMERGED = "closed_unmerged"
PR_STATE_UNKNOWN = "unknown"

#: States whose code may ground a gradeable test case.
GROUNDING_STATES = frozenset({PR_STATE_MERGED, PR_STATE_OPEN, PR_STATE_UNKNOWN})

#: States whose code is authoritative — what shipped, and what a failing
#: case can therefore be blamed on.
AUTHORITATIVE_STATES = frozenset({PR_STATE_MERGED})

_PR_URL_NUMBER_RE = re.compile(r"/pull/(\d+)")

# Jira's dev-status API mirrors the provider's state under its own
# vocabulary and can lag behind GitHub; "DECLINED" is its word for a PR
# closed without merging. GitHub's own `state` is "open"/"closed" with a
# separate `merged` boolean.
_STATUS_ALIASES = {
    "merged": PR_STATE_MERGED,
    "open": PR_STATE_OPEN,
    "declined": PR_STATE_CLOSED_UNMERGED,
    "closed": PR_STATE_CLOSED_UNMERGED,
    "closed_unmerged": PR_STATE_CLOSED_UNMERGED,
    "rejected": PR_STATE_CLOSED_UNMERGED,
    "abandoned": PR_STATE_CLOSED_UNMERGED,
}


def classify_pr_state(pr: dict) -> str:
    """Normalize one PR dict to a state from the four above.

    ``merged_at`` wins over the status string: it is only ever set from
    GitHub's own payload, so a PR that landed is treated as merged even
    when Jira's mirror still says OPEN. Everything unrecognized becomes
    ``unknown`` rather than being guessed into a decision.
    """
    if not isinstance(pr, dict):
        return PR_STATE_UNKNOWN
    merged_at = pr.get("merged_at")
    if isinstance(merged_at, str) and merged_at.strip():
        return PR_STATE_MERGED
    raw = pr.get("status")
    if not isinstance(raw, str):
        return PR_STATE_UNKNOWN
    return _STATUS_ALIASES.get(raw.strip().lower(), PR_STATE_UNKNOWN)


def pr_number(pr: dict) -> int | None:
    """PR number, from the explicit field or parsed out of the URL."""
    if not isinstance(pr, dict):
        return None
    raw = pr.get("number")
    if isinstance(raw, int):
        return raw
    url = pr.get("url")
    if isinstance(url, str):
        match = _PR_URL_NUMBER_RE.search(url)
        if match:
            return int(match.group(1))
    return None


def _provenance_entry(pr: dict, state: str) -> dict:
    """One row of the per-run provenance record.

    Deliberately flat and JSON-safe: this is persisted to ``runs`` and
    read back by whoever is asking "what was this plan derived from?",
    possibly long after the PR itself has changed state again.
    """
    return {
        "number": pr_number(pr),
        "repository": pr.get("repository"),
        "url": pr.get("url"),
        "title": pr.get("title"),
        "state": state,
        "head_sha": pr.get("head_sha"),
        "merged_at": pr.get("merged_at"),
        "source_branch": pr.get("source_branch"),
        "used_as_grounding": state in GROUNDING_STATES,
    }


def partition_pull_requests(
    pull_requests: list | None,
) -> tuple[list[dict], list[dict]]:
    """Split PRs into (usable for grounding, excluded).

    Excluded means exactly one thing today: closed without merging.
    """
    usable: list[dict] = []
    excluded: list[dict] = []
    for pr in pull_requests or []:
        if not isinstance(pr, dict):
            continue
        if classify_pr_state(pr) in GROUNDING_STATES:
            usable.append(pr)
        else:
            excluded.append(pr)
    return usable, excluded


def _commit_shas(dev_info: dict) -> list[str]:
    """SHAs of the commits Jira linked to the ticket, newest-first order
    preserved. Parsed from the commit URL, the only place dev-status puts
    them."""
    shas: list[str] = []
    for commit in dev_info.get("commits") or []:
        if not isinstance(commit, dict):
            continue
        url = commit.get("url")
        if not isinstance(url, str):
            continue
        match = re.search(r"/commits?/([0-9a-f]{7,40})", url)
        if match and match.group(1) not in shas:
            shas.append(match.group(1))
    return shas


def build_provenance(dev_info: dict | None) -> dict:
    """Record what a run was (and wasn't) allowed to look at.

    Includes excluded PRs — a reader asking why a plan is thin needs to
    see the abandoned PR that was deliberately skipped, not an absence.
    """
    dev_info = dev_info if isinstance(dev_info, dict) else {}
    entries: list[dict] = []
    counts = {
        PR_STATE_MERGED: 0,
        PR_STATE_OPEN: 0,
        PR_STATE_CLOSED_UNMERGED: 0,
        PR_STATE_UNKNOWN: 0,
    }
    for pr in dev_info.get("pull_requests") or []:
        if not isinstance(pr, dict):
            continue
        state = classify_pr_state(pr)
        counts[state] = counts.get(state, 0) + 1
        entries.append(_provenance_entry(pr, state))

    grounding = [e for e in entries if e["used_as_grounding"]]
    return {
        "pull_requests": entries,
        "commit_shas": _commit_shas(dev_info),
        "branches": [b for b in (dev_info.get("branches") or []) if isinstance(b, str)],
        "merged_pr_count": counts[PR_STATE_MERGED],
        "open_pr_count": counts[PR_STATE_OPEN],
        "excluded_pr_count": counts[PR_STATE_CLOSED_UNMERGED],
        "state_unverified_pr_count": counts[PR_STATE_UNKNOWN],
        # True when the plan leans on code that has not landed, so the
        # reader knows the code can still move underneath the cases.
        "grounded_on_unmerged": any(
            e["state"] in (PR_STATE_OPEN, PR_STATE_UNKNOWN) for e in grounding
        ),
        "authoritative": bool(grounding) and all(
            e["state"] in AUTHORITATIVE_STATES for e in grounding
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def merge_provenance(per_ticket: list[tuple[str, dict]]) -> dict:
    """Fold several tickets' provenance into one record for a multi-ticket run.

    Same top-level shape as ``build_provenance`` so a reader (or a
    renderer) doesn't need to branch on run type, plus ``by_ticket`` for
    the cases where attribution matters. Each PR entry gains the ticket
    key it came from.
    """
    merged: dict = {
        "pull_requests": [],
        "commit_shas": [],
        "branches": [],
        "merged_pr_count": 0,
        "open_pr_count": 0,
        "excluded_pr_count": 0,
        "state_unverified_pr_count": 0,
        "grounded_on_unmerged": False,
        "by_ticket": {},
    }
    for ticket_key, prov in per_ticket:
        merged["by_ticket"][ticket_key] = prov
        for entry in prov.get("pull_requests") or []:
            merged["pull_requests"].append({**entry, "ticket_key": ticket_key})
        for sha in prov.get("commit_shas") or []:
            if sha not in merged["commit_shas"]:
                merged["commit_shas"].append(sha)
        for branch in prov.get("branches") or []:
            if branch not in merged["branches"]:
                merged["branches"].append(branch)
        for key in (
            "merged_pr_count",
            "open_pr_count",
            "excluded_pr_count",
            "state_unverified_pr_count",
        ):
            merged[key] += prov.get(key, 0)
        merged["grounded_on_unmerged"] = merged["grounded_on_unmerged"] or bool(
            prov.get("grounded_on_unmerged")
        )
    grounding = [e for e in merged["pull_requests"] if e.get("used_as_grounding")]
    merged["authoritative"] = bool(grounding) and all(
        e.get("state") in AUTHORITATIVE_STATES for e in grounding
    )
    merged["generated_at"] = datetime.now(timezone.utc).isoformat()
    return merged


def has_grounding_source(provenance: dict) -> bool:
    """Whether any PR survived the state filter."""
    return any(
        e.get("used_as_grounding") for e in (provenance.get("pull_requests") or [])
    )


def filter_development_info(dev_info: dict | None) -> tuple[dict | None, dict]:
    """Strip non-grounding PRs out of ``dev_info`` and record provenance.

    Returns ``(filtered_dev_info, provenance)``. The filtered dict is what
    reaches the prompt and all four critics; the provenance is what
    reaches the run record and the rendered plan.

    A branch is dropped along with the PR it belongs to, so an abandoned
    branch name can't be quoted back into a test step. ``repository_context``
    and ``figma_context`` survive untouched — a README and a design file
    are not claims about what shipped.
    """
    provenance = build_provenance(dev_info)
    if not isinstance(dev_info, dict):
        return dev_info, provenance

    usable, excluded = partition_pull_requests(dev_info.get("pull_requests"))
    if not excluded:
        return dev_info, provenance

    kept_branches = {
        pr.get("source_branch") for pr in usable if pr.get("source_branch")
    }
    dropped_branches = {
        pr.get("source_branch")
        for pr in excluded
        if pr.get("source_branch") and pr.get("source_branch") not in kept_branches
    }

    filtered = dict(dev_info)
    filtered["pull_requests"] = usable
    filtered["branches"] = [
        b for b in (dev_info.get("branches") or []) if b not in dropped_branches
    ]
    return filtered, provenance


def searched_surfaces(provenance: dict) -> list[str]:
    """Human-readable list of what was looked at, for the no-source result.

    "We found nothing" is only useful next to "here is where we looked" —
    otherwise the reader can't tell a missing PR from a broken integration.
    """
    searched = [
        "Jira dev-status linked pull requests",
        "GitHub PR links in the ticket description and comments",
        "Jira dev-status linked branches",
        "Jira dev-status linked commits",
    ]
    excluded = [
        e
        for e in (provenance.get("pull_requests") or [])
        if not e.get("used_as_grounding")
    ]
    if excluded:
        searched.append(
            "closed-unmerged pull requests (found, and deliberately not used): "
            + ", ".join(_describe_pr(e) for e in excluded)
        )
    return searched


def _describe_pr(entry: dict) -> str:
    repo = entry.get("repository") or ""
    number = entry.get("number")
    if repo and number:
        return f"{repo}#{number}"
    return entry.get("url") or entry.get("title") or "unidentified PR"


NO_SOURCE_MESSAGE = (
    "No implementation was found for this ticket, so no test plan was "
    "generated. A plan written without source is speculation that reads as "
    "authoritative — the failure mode this check exists to prevent. Link a "
    "pull request, or attach a spec describing the intended behaviour, and "
    "regenerate."
)


def no_source_result(ticket_key: str, provenance: dict) -> dict:
    """The short result that replaces a plan when nothing grounds it.

    Shaped like a plan response (same section keys, all empty) so every
    consumer — the UI, the Jira poster, the CLI, the MCP server — can
    render it without a special case, but carries ``no_source: True`` for
    the ones that want to say something better than "0 cases".
    """
    return {
        "ticket_key": ticket_key,
        "no_source": True,
        "no_source_message": NO_SOURCE_MESSAGE,
        "searched": searched_surfaces(provenance),
        "source_provenance": provenance,
        "happy_path": [],
        "edge_cases": [],
        "integration_tests": [],
        "regression_checklist": [],
        "needs_spec_cases": [],
        "ac_coverage": None,
        "grounding_warnings": [],
        "risks_and_gaps": [],
        "uat_complexity": None,
        "how_to_see_it": None,
    }
