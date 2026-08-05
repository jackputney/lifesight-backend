"""Public API bounds for Mail & Calendar read endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

MAX_MAIL_PAGE_SIZE = 50
MAX_EVENTS_PAGE_SIZE = 100
MAX_SEARCH_QUERY_LENGTH = 500
MAX_RANGE_DAYS = 62
DEFAULT_MAIL_PAGE_SIZE = 20
DEFAULT_EVENTS_PAGE_SIZE = 50


class BoundsError(ValueError):
    pass


def clamp_page_size(value: int, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    return max(1, min(int(value), maximum))


def clamp_search_query(q: str | None) -> str | None:
    if q is None:
        return None
    text = q.strip()
    if not text:
        return None
    if len(text) > MAX_SEARCH_QUERY_LENGTH:
        raise BoundsError(
            f"Search query exceeds {MAX_SEARCH_QUERY_LENGTH} characters"
        )
    return text


def parse_rfc3339(value: str) -> datetime:
    raw = (value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BoundsError("Invalid RFC3339 timestamp") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_time_range(time_min: str, time_max: str) -> tuple[str, str]:
    start = parse_rfc3339(time_min)
    end = parse_rfc3339(time_max)
    if end <= start:
        raise BoundsError("time_max must be after time_min")
    if end - start > timedelta(days=MAX_RANGE_DAYS):
        raise BoundsError(f"Date range exceeds {MAX_RANGE_DAYS} days")
    return (
        start.isoformat().replace("+00:00", "Z"),
        end.isoformat().replace("+00:00", "Z"),
    )
