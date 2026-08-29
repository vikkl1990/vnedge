"""Deterministic ordering for quote events shared by live and replay."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def sequence_order_key(sequence: int | str | None) -> tuple[int, int | str]:
    """Return a total-order key without comparing integers to strings."""
    if sequence is None or sequence == "":
        return (2, "")
    try:
        return (0, int(sequence))
    except (TypeError, ValueError):
        return (1, str(sequence))


def quote_order_key(
    event_ts: datetime,
    received_ts: datetime | None,
    sequence: int | str | None,
    ordinal: int = 0,
) -> tuple[datetime, datetime, tuple[int, int | str], int]:
    """Match event, receipt, native-sequence, then stable input order."""
    return (
        event_ts,
        received_ts or event_ts,
        sequence_order_key(sequence),
        ordinal,
    )


def quote_update_order_key(item: Any) -> tuple[datetime, datetime, tuple[int, int | str], int]:
    """Adapter for ``QuoteUpdate`` without importing the feed module."""
    return quote_order_key(
        item.ts,
        getattr(item, "received_ts", None),
        getattr(item, "sequence", None),
    )


__all__ = ["quote_order_key", "quote_update_order_key", "sequence_order_key"]
