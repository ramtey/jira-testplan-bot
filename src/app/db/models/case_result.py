from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from src.app.db.base import DocumentBase

# What happened to a case. `cleared` is a person withdrawing an earlier mark:
# results are append-only, so taking a tick back is a row of its own.
CaseStatus = Literal["passed", "failed", "not_run", "cleared"]

# Who produced a result. `legacy_unknown` is a mark recorded before results
# had actors, or written through the old whole-set PUT, where the server cannot
# tell a person from a script. It is never guessed to be human.
ActorKind = Literal["human", "agent", "legacy_unknown"]


class CaseResult(DocumentBase):
    """One recorded outcome for one test case. Append-only.

    The checked set a tester sees is derived from these rows (plus the legacy
    marks frozen in ``test_plan_progress``), never stored beside them. Writers
    only insert, so two of them — a tester in the browser and a runner marking
    as it goes — can no longer overwrite each other's marks, which the old
    whole-set save did.

    ``progress_key`` is the key the result is filed under, obtained from the
    server's one producer. ``plan_id`` is the plan the case was read from, kept
    so a reader can tell results for different plans that share a key apart.
    """

    __collection__: ClassVar[str] = "case_results"

    progress_key: str
    plan_id: int | None = None
    case_id: str
    status: CaseStatus
    # {"code": ..., "detail": ...} when status is not_run.
    not_run: dict[str, Any] | None = None
    # {"kind": ActorKind, "user_id": int | None, "email": str | None, "name": str | None}
    actor: dict[str, Any]
    # Where the write came from: "ui", "put", "carryover", "runner".
    source: str
    # [{"kind": "assertion" | "screenshot" | "video_ts" | "unit_test" | "http",
    #   "ref": ..., "at_s": ..., "text": ...}]
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    run_id: int | None = None
    # An agent's result changes the checked set only once a person approves it.
    review_approved: bool = False
    # {"plan_id": ..., "case_id": ...} when carried over from a regenerated plan.
    carried_from: dict[str, Any] | None = None
