from __future__ import annotations

from typing import ClassVar

from src.app.db.base import DocumentBase


class TestPlanProgress(DocumentBase):
    """Shared, per-ticket record of which test cases a QA team has checked off.

    Keyed by ``progress_key`` — the same composite the frontend builds from the
    ticket key(s) plus a fingerprint of the plan's section sizes. Encoding the
    fingerprint means a regenerated plan with a different shape gets a fresh key,
    so stale checks never carry over onto a different set of cases.

    Deliberately *not* keyed by user: progress is shared by everyone testing the
    ticket (mirroring how the walkthrough is one document per ticket), so the
    whole team sees the same "13/39" regardless of who ticked the boxes.

    ``checked_ids`` remains a JSON-encoded string for the same reason as
    ``TicketWalkthrough.screenshots`` — the frontend parses it as one.
    """

    __collection__: ClassVar[str] = "test_plan_progress"

    progress_key: str
    # JSON array of checked test-case ids, e.g. ["happy_path:0", "edge_cases:2"].
    checked_ids: str = "[]"
