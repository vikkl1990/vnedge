"""Chart series for the operator cockpit: canonical candles plus trade markers.

Two things the pulse endpoints do not provide and forensics needs:

* raw OHLCV from the CANONICAL store, so what is charted is the same series
  research and shadow are supposed to read -- not a fourth source invented for
  the UI;
Markers are deliberately NOT served here. MarketPulse already builds them from
journal scanner_events and calls setMarkers itself; a second server-side path
would be two things doing one job, which is the exact defect the 2026-08-22
audit catalogued.

Read-only. This module has no order, promotion or settings authority.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any

UTC = timezone.utc

#: Never hand an unbounded series to a browser; a year of 1m bars is 525k rows.
MAX_BARS = 5_000


def candles_payload(store: Any, symbol: str, timeframe: str, *,
                    limit: int = 500) -> dict[str, Any]:
    """Canonical OHLCV shaped for lightweight-charts.

    Decimals become floats only at this boundary. They stay Decimal everywhere
    the money math happens; JSON has no decimal type and the chart needs none.
    """
    limit = max(1, min(int(limit), MAX_BARS))
    try:
        rows = list(store.read(symbol, timeframe))
    except Exception:
        rows = []

    # Lightweight Charts requires strictly monotonic series input.  The store
    # is normally ordered and idempotent, but recovery/upsert boundaries can
    # briefly expose duplicate or unsorted rows.  Make the HTTP boundary safe:
    # one authoritative row per epoch, ascending, before taking the tail.
    by_time: dict[int, Any] = {}
    for candle in rows:
        epoch = (
            int(candle.open_time.replace(tzinfo=UTC).timestamp())
            if candle.open_time.tzinfo is None
            else int(candle.open_time.timestamp())
        )
        by_time[epoch] = candle
    ordered = sorted(by_time.items())
    tail = ordered[-limit:]
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "source": "canonical_lake",
        "count": len(tail),
        "truncated": len(ordered) > len(tail),
        "candles": [
            {
                "time": epoch,
                "open": float(c.open), "high": float(c.high),
                "low": float(c.low), "close": float(c.close),
                "volume": float(c.volume),
            }
            for epoch, c in tail
        ],
    }
