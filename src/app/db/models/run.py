from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar

from bson import Decimal128
from pydantic import field_serializer, field_validator

from src.app.db.base import DocumentBase


class RunType(str, Enum):
    test_plan = "test_plan"
    test_plan_multi = "test_plan_multi"
    bug_lens = "bug_lens"
    bug_lens_multi = "bug_lens_multi"
    summarize = "summarize"


class RunStatus(str, Enum):
    ok = "ok"
    error = "error"


class Run(DocumentBase):
    __collection__: ClassVar[str] = "runs"

    user_id: int

    run_type: RunType
    status: RunStatus

    ticket_keys: list[str] = []

    model: str
    llm_provider: str

    latency_ms: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    # Stored as BSON Decimal128 so fractional cents survive a round trip exactly
    # and the field stays summable in an aggregation. Postgres held this as
    # NUMERIC(10, 6); Decimal128 is the equivalent that BSON understands, and a
    # plain float would quietly lose precision at this many decimal places.
    cost_usd: Decimal = Decimal("0")

    error_code: str | None = None

    had_pr_diff: bool = False
    had_figma: bool = False
    had_parent: bool = False
    linked_ticket_count: int = 0
    pr_count: int = 0
    comment_count: int = 0

    # What the plan was actually derived from: every PR the run saw, its
    # state at generation time, its head SHA, and whether that state let it
    # ground a case. Without this a reader of an old plan cannot tell
    # whether a case came from code that shipped, code still in review, or
    # code that was abandoned before the plan was written.
    source_provenance: dict | None = None

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _coerce_cost(cls, value: Any) -> Any:
        if isinstance(value, Decimal128):
            return value.to_decimal()
        if isinstance(value, float):
            return Decimal(str(value))
        return value

    # Returns Any rather than Decimal128: pydantic builds a serialization schema
    # from the annotation, and it has no schema for a bson type.
    @field_serializer("cost_usd")
    def _dump_cost(self, value: Decimal) -> Any:
        return Decimal128(Decimal(str(value)))
