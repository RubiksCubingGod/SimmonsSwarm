"""Readable Pacific times for pages people read. Records keep UTC ISO strings."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_PACIFIC = "America/Los_Angeles"


def pacific(value: object) -> str | None:
    """Format a UTC ISO timestamp for a person, in Pacific time.

    ``"2026-10-04T18:02:42.069Z"`` becomes
    ``"Sunday, October 4, 2026 at 11:02 AM PDT"``; the abbreviation follows
    daylight saving (PDT in summer, PST in winter). Returns None for anything
    that is not a timestamp, and the input unchanged if no time-zone data is
    installed.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        local = moment.astimezone(ZoneInfo(_PACIFIC))
    except ZoneInfoNotFoundError:
        return value
    hour = local.hour % 12 or 12
    return (
        f"{local:%A}, {local:%B} {local.day}, {local.year} "
        f"at {hour}:{local:%M} {local:%p} {local.tzname()}"
    )


__all__ = ["pacific"]
