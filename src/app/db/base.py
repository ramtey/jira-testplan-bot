from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    """Now, in UTC, truncated to milliseconds.

    BSON stores datetimes at millisecond precision, so a microsecond-precision
    value silently loses its last three digits on the way to Mongo. Postgres kept
    them, so before the cutover an in-memory timestamp and its reloaded copy
    compared equal; afterwards they would not, and a stamp could appear to move
    backwards across a reload. Truncating on creation makes what the app holds and
    what the database can store the same value.
    """
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


TDoc = TypeVar("TDoc", bound="DocumentBase")


def _as_aware(value: Any) -> Any:
    """Re-attach UTC to a naive datetime coming back from Mongo.

    The client is opened with ``tz_aware=True`` so this is normally a no-op, but
    documents written by the backfill or by an older client can still surface
    naive values, and the app compares these against ``datetime.now(timezone.utc)``.
    A naive/aware comparison raises TypeError, so normalize on the way in.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class DocumentBase(BaseModel):
    """Base for every persisted document.

    Mirrors the old ``TimestampedBase``: an integer primary key plus created/updated
    stamps. ``id`` maps to Mongo's ``_id`` — kept as an int rather than an ObjectId
    so existing API routes, frontend URLs and plan-version chains keep working
    unchanged across the cutover.
    """

    model_config = ConfigDict(use_enum_values=False, validate_assignment=False)

    # Set by subclasses; names the Mongo collection this document lives in.
    __collection__: ClassVar[str] = ""

    id: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @classmethod
    def from_doc(cls: type[TDoc], doc: dict[str, Any] | None) -> TDoc | None:
        if doc is None:
            return None
        data = {k: _as_aware(v) for k, v in doc.items() if k != "_id"}
        data["id"] = doc.get("_id")
        return cls.model_validate(data)

    def to_doc(self) -> dict[str, Any]:
        """Serialize for Mongo. Enums become their values, and ``id`` becomes ``_id``.

        ``mode="json"`` would stringify datetimes, which Mongo stores natively and
        the app reads back as datetimes, so this uses python mode and lets the
        driver encode. Decimal is handled by each model's own serializer.
        """
        data = self.model_dump(mode="python", exclude={"id"})
        for key, value in list(data.items()):
            if hasattr(value, "value") and hasattr(value, "name"):
                data[key] = value.value
        if self.id is not None:
            data["_id"] = self.id
        return data
