"""Stable content identity for immutable market-data bars.

This module deliberately has no strategy or runtime dependencies.  The lake
writer, router parity checker, and decision-envelope boundary must hash the
same normalized payload or they are not talking about the same bar.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def canonical_number(value: object) -> str | None:
    """Normalize Python, pandas, and Decimal scalars for stable hashing."""
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    normalized = number.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def bar_content_sha256(
    row: Mapping[str, Any],
    *,
    open_time: datetime,
    close_time: datetime,
    source: str,
) -> str:
    """Hash canonical OHLCV content and its provenance.

    Exchange, symbol, and timeframe remain the surrounding lookup identity.
    This keeps router/Parquet comparison focused on whether both transports
    published the same bar bytes for that identity.
    """
    identity: dict[str, object] = {
        "open_time": open_time.isoformat(),
        "close_time": close_time.isoformat(),
        "open": canonical_number(row.get("open")),
        "high": canonical_number(row.get("high")),
        "low": canonical_number(row.get("low")),
        "close": canonical_number(row.get("close")),
        "volume": canonical_number(row.get("volume")),
        "quote_volume": canonical_number(row.get("quote_volume")),
        "trade_count": canonical_number(row.get("trade_count")),
        "source": source,
    }
    # Compatibility-only provenance for old synthetic fixtures whose raw
    # timestamp was not aligned to the declared timeframe.
    if row.get("source_open_time") is not None:
        identity["source_open_time"] = str(row["source_open_time"])
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


__all__ = ["bar_content_sha256", "canonical_number"]
