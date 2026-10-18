"""When the queue watcher is allowed to sweep.

`WATCH_ACTIVE_DAYS` (e.g. "mon-fri") and `WATCH_ACTIVE_HOURS` (e.g.
"07:00-18:00"), in the machine's local time. Empty means always, which is
the default so a team that never sets them sees no change.

Three things read this, and all three have to agree, which is why it lives
in one place:

* the sweep, which does nothing outside the window;
* the auto-post sweep's lookback, which must reach back across the whole
  off period — Friday 18:00 to Monday 07:00 is 61 hours, and a 24-hour
  lookback would silently drop a ticket dragged into testing on Friday night;
* /health/watcher, which must not call a watcher "stopped" for keeping to
  its schedule, and must measure lateness from when the window opened.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from .config import settings

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class ScheduleError(ValueError):
    """The configured days or hours cannot be read."""


def _parse_days(text: str) -> set[int]:
    """'mon-fri' / 'mon,wed,fri' / 'sat-sun' to weekday numbers (mon=0)."""
    days: set[int] = set()
    for part in text.lower().replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            if start not in _DAYS or end not in _DAYS:
                raise ScheduleError(f"unknown day in {part!r}")
            i, j = _DAYS.index(start), _DAYS.index(end)
            span = range(i, j + 1) if i <= j else [*range(i, 7), *range(0, j + 1)]
            days.update(span)
        else:
            if part not in _DAYS:
                raise ScheduleError(f"unknown day {part!r}")
            days.add(_DAYS.index(part))
    return days


def _parse_hours(text: str) -> tuple[time, time]:
    try:
        start, end = (time.fromisoformat(p.strip()) for p in text.split("-", 1))
    except ValueError as exc:
        raise ScheduleError(f"hours must look like 07:00-18:00, got {text!r}") from exc
    if not start < end:
        raise ScheduleError(f"hours must start before they end, got {text!r}")
    return start, end


def _config() -> tuple[set[int], tuple[time, time]] | None:
    days_text = (settings.watch_active_days or "").strip()
    hours_text = (settings.watch_active_hours or "").strip()
    if not days_text and not hours_text:
        return None
    days = _parse_days(days_text) if days_text else set(range(7))
    hours = _parse_hours(hours_text) if hours_text else (time(0, 0), time.max)
    return days, hours


def current_time() -> datetime:
    """Now, in local time. The one clock every check here reads."""
    return datetime.now().astimezone()


def _now(now: datetime | None) -> datetime:
    return now.astimezone() if now else current_time()


def describe() -> str:
    """The schedule in words, for logs and the CLI."""
    cfg = _config()
    if cfg is None:
        return "always"
    return f"{settings.watch_active_days or 'every day'} {settings.watch_active_hours or 'all day'}"


def is_active(now: datetime | None = None) -> bool:
    cfg = _config()
    if cfg is None:
        return True
    days, (start, end) = cfg
    now = _now(now)
    return now.weekday() in days and start <= now.time() < end


def window_start(now: datetime | None = None) -> datetime | None:
    """When the window containing `now` opened, or None outside one."""
    cfg = _config()
    now = _now(now)
    if cfg is None or not is_active(now):
        return None
    _days, (start, _end) = cfg
    return now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)


def _previous_window_end(before: datetime) -> datetime | None:
    cfg = _config()
    if cfg is None:
        return None
    days, (_start, end) = cfg
    day = before
    for _ in range(8):
        day = day - timedelta(days=1)
        if day.weekday() in days:
            return day.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    return None


def lookback_hours(configured: int, now: datetime | None = None) -> int:
    """How far the auto-post sweep must look back, in whole hours.

    At least `configured`, and inside a window at least back to when the
    previous window closed, plus an hour of margin. Measured from now rather
    than from when this window opened, so a first sweep of the morning that
    failed (Jira down at 07:00) is still covered by the 09:00 one.
    """
    opened = window_start(now)
    if opened is None:
        return configured
    ended = _previous_window_end(opened)
    if ended is None:
        return configured
    since = (_now(now) - ended).total_seconds() / 3600
    return max(configured, int(since) + 2)
