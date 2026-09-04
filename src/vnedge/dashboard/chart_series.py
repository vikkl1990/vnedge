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


def mechanism_context_payload(store: Any, symbol: str, timeframe: str, *,
                              limit: int = 600) -> dict[str, Any]:
    """Drawable mechanism context for the chart, from the canonical store.

    Same store discipline as ``candles_payload``; the context itself comes
    from ``ml.mechanism_features.mechanism_context`` — the model's own
    definitions — so the chart can never show the operator a different
    market than the ML plane sees. Read-only, presentation-only.
    """
    import pandas as pd

    from vnedge.ml.mechanism_features import mechanism_context

    limit = max(1, min(int(limit), MAX_BARS))
    try:
        rows = list(store.read(symbol, timeframe))
    except Exception:
        rows = []
    base = {"symbol": symbol, "timeframe": timeframe, "source": "canonical_lake"}
    if not rows:
        return {**base, "ready": False, "bars": 0}
    rows = rows[-limit:]
    frame = pd.DataFrame(
        {
            "timestamp": [candle.open_time for candle in rows],
            "open": [float(candle.open) for candle in rows],
            "high": [float(candle.high) for candle in rows],
            "low": [float(candle.low) for candle in rows],
            "close": [float(candle.close) for candle in rows],
            "volume": [float(candle.volume) for candle in rows],
        }
    )
    context = mechanism_context(frame)
    last = rows[-1]
    epoch = (
        int(last.open_time.replace(tzinfo=UTC).timestamp())
        if last.open_time.tzinfo is None
        else int(last.open_time.timestamp())
    )
    return {**base, "as_of": epoch, **context}
