from __future__ import annotations

from datetime import UTC, datetime


def utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def utc_from_timestamp_naive(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC).replace(tzinfo=None)
