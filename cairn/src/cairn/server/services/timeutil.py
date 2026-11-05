"""UTC timestamps in the canonical Cairn wire format."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow_before(seconds: int) -> str:
    moment = datetime.now(UTC) - timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
