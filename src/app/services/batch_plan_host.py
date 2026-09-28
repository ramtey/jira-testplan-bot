"""Pick the one ticket in a batch that carries the plan.

A batch plan used to be posted to every ticket it covered. The text was
byte-identical each time, so seven tickets meant seven copies of one plan to
keep in step — and because a plan can occupy several Jira comments, a large
batch could write dozens of comments to say one thing. Instead the plan goes to
a single *host* ticket and the rest get a short pointer to it.

Which ticket hosts is decided here, once, and then recorded on the run. The
recording is the important half: the signals below move as work proceeds (a
ticket gains branches), so recomputing the host on every post would eventually
move it, and a moved host means the previous host's comment is orphaned while a
second copy appears elsewhere — the duplication this exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class BatchHostCandidate:
    """One ticket's claim to hosting the batch plan."""

    key: str
    # The key of this ticket's parent/epic, when it has one. Used to spot a
    # parent that is itself part of the batch.
    parent_key: str | None = None
    # How much linked development work Jira's panel shows against this ticket —
    # the stand-in for "where the work actually landed".
    #
    # Both counts, not just branches, because branches alone do not
    # discriminate here: across SK-2623..SK-2665 every ticket reported zero
    # branches while their PR counts ran from 1 to 18. `_extract_branches`
    # reads the `repository` dev-status detail, which this Jira does not
    # populate; the `pullrequest` detail is the one that carries the work. A
    # rule that always scores zero is not a tie-break, it is no rule at all.
    branch_count: int = 0
    pr_count: int = 0

    @property
    def linked_work(self) -> int:
        """What rule 2 ranks on. A PR normally implies a branch, so where both
        are populated this double-counts — equally for every candidate, which
        leaves the ordering untouched."""
        return self.branch_count + self.pr_count


_KEY_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)-(\d+)$")


def _key_sort_value(key: str) -> tuple[str, int, str]:
    """Order ticket keys the way a reader would: project, then number.

    Plain string ordering puts SK-10 before SK-9, which would make the
    tie-break look arbitrary to anyone checking why a particular ticket won.
    A key that doesn't parse sorts last by its own text rather than crashing —
    the tie-break only has to be stable and explicable, not clever.
    """
    match = _KEY_PATTERN.match(key.strip())
    if not match:
        return ("￿", 0, key)
    return (match.group(1).upper(), int(match.group(2)), key)


def choose_batch_host(candidates: Sequence[BatchHostCandidate]) -> str:
    """The key of the ticket that should carry the batch plan.

    In order:

    1. A parent/epic that is itself in the batch wins. It is the ticket a
       reader already treats as the home of the group, and it outlives the
       children — a plan hosted on a child is stranded when that child closes.
    2. Otherwise the ticket with the most linked development work — branches
       plus pull requests — as the best available proxy for where the work is.
    3. Ties break on the lowest ticket key, which is stable across runs.
       Anything drawn from the batch's own ordering would not be: the browser
       sends whatever order the keys were typed in.

    Rule 1 is deliberately absolute — a parent in the batch hosts even when a
    child carries far more branches.
    """
    if not candidates:
        raise ValueError("choose_batch_host needs at least one candidate")

    keys = {c.key for c in candidates}
    parents_in_batch = [
        c for c in candidates
        if any(other.parent_key == c.key for other in candidates if other.key != c.key)
    ]
    pool = parents_in_batch or list(candidates)

    # Guard against a parent chain inside one batch (a child of a child).
    # Whichever of them is nobody else's child is the top of the group.
    if len(pool) > 1 and parents_in_batch:
        topmost = [c for c in pool if not (c.parent_key and c.parent_key in keys)]
        pool = topmost or pool

    return min(pool, key=lambda c: (-c.linked_work, _key_sort_value(c.key))).key


def candidate_from_ticket(ticket) -> BatchHostCandidate:
    """Read a `TicketInput`-shaped object into a host candidate.

    Both fields are optional on the way in and absent on plenty of real
    tickets, so a missing parent or an unreadable development panel degrades
    to "no claim" rather than failing the post.
    """
    parent_info = getattr(ticket, "parent_info", None) or {}
    parent_key = parent_info.get("key") if isinstance(parent_info, dict) else None

    dev_info = getattr(ticket, "development_info", None) or {}
    if not isinstance(dev_info, dict):
        dev_info = {}

    def _count(field: str) -> int:
        value = dev_info.get(field)
        return len(value) if isinstance(value, (list, tuple)) else 0

    return BatchHostCandidate(
        key=ticket.ticket_key,
        parent_key=parent_key.strip().upper() if isinstance(parent_key, str) else None,
        branch_count=_count("branches"),
        # Counted after `filter_development_info` has run, so a PR that was
        # closed without merging does not argue for hosting the plan on the
        # ticket that abandoned it.
        pr_count=_count("pull_requests"),
    )


def choose_batch_host_for_tickets(tickets: Iterable) -> str:
    """`choose_batch_host` over `TicketInput`-shaped objects."""
    return choose_batch_host([candidate_from_ticket(t) for t in tickets])
