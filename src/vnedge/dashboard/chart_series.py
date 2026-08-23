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
        rows = store.read(symbol, timeframe)
    except Exception:
        rows = []
    tail = list(rows)[-limit:]
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "source": "canonical_lake",
        "count": len(tail),
        "truncated": len(rows) > len(tail),
        "candles": [
            {
                "time": int(c.open_time.replace(tzinfo=UTC).timestamp())
                if c.open_time.tzinfo is None
                else int(c.open_time.timestamp()),
                "open": float(c.open), "high": float(c.high),
                "low": float(c.low), "close": float(c.close),
                "volume": float(c.volume),
            }
            for c in tail
        ],
    }
