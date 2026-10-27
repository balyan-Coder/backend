"""Time helpers.  All database timestamps are UTC ISO-8601 strings."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    value = value or utcnow()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def start_of_day(value: date | None = None) -> str:
    value = value or utcnow().date()
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc).isoformat(
        timespec="seconds"
    )


def end_of_day(value: date | None = None) -> str:
    value = value or utcnow().date()
    start = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return (start + timedelta(days=1)).isoformat(timespec="seconds")


def period_start(period: str) -> str:
    days = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}.get(period, 30)
    today = utcnow().date()
    return start_of_day(today - timedelta(days=days - 1))


def month_key(value: str) -> str:
    return value[:7]
