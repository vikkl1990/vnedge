"""Range-breakout HF engine — target the larger structural moves, not 1s noise.

The hypothesis behind the session/range proposal: the usable part of the daily
range appears when (a) it's the active session and (b) the recent range is already
elevated, and price breaks the rolling N-minute high/low. Unlike 1s imbalance, this
targets 40-100bps structural moves, and its edge estimate is scaled to the recent
range so it CLEARS the CostGate — which means the profit ladder / horizon tools now
test the real question: does a breakout actually CONTINUE, or is it a false break?

SCAFFOLD — a signal that clears the CostGate is NOT a validated edge; realized drift
(profit ladder) is the judge. Deterministic under ordered replay (state = the rolling
mid window + last-signal ts). Session + range are HARD gates.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from decimal import Decimal
from typing import Any, Deque, Dict, Optional, Sequence

from vnedge.strategy.signal_engine import (
    SignalEngine,
    SignalIntent,
    TickSnapshot,
    make_signal_id,
)


class RangeBreakoutEngine(SignalEngine):
    """Break of the rolling ``window_sec`` high/low, gated to active hours + an
    elevated recent range. Seven knobs (window/min_range/active_hours/stop/tp/hold/
    edge_frac)."""

    engine_id = "RangeBreakoutEngine"

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        window_sec: int = 3600,                         # rolling high/low lookback (1h)
        min_range_bps: Decimal = Decimal("60"),         # only trade elevated-range regimes
        active_hours: Sequence[int] = (12, 13, 14, 15, 16, 17),  # UTC session
        stop_bps: Decimal = Decimal("30"),
        tp_bps: Decimal = Decimal("45"),
        max_holding_sec: int = 900,                     # 15 min
        edge_frac: Decimal = Decimal("0.4"),            # expected continuation ≈ frac × range
        cooldown_sec: float = 60.0,
        min_window_pts: int = 300,
    ):
        self.symbol = symbol
        self.window_sec = window_sec
        self.min_range_bps = min_range_bps
        self.active_hours = set(active_hours)
        self.stop_bps = stop_bps
        self.tp_bps = tp_bps
        self.max_holding_sec = max_holding_sec
        self.edge_frac = edge_frac
        self.cooldown_sec = cooldown_sec
        self.min_window_pts = min_window_pts
        self._win: Deque[tuple[float, float]] = deque()   # (ts_sec, mid)
        self._last_signal_ts: Optional[datetime] = None

    def generate(
        self,
        tick: TickSnapshot,
        account_equity: Decimal,
        open_positions: Sequence[Dict[str, Any]],
    ) -> Sequence[SignalIntent]:
        if tick.symbol != self.symbol:
            return []
        ts = tick.ts.timestamp()
        mid = float(tick.mid)

        # rolling window extremes BEFORE adding this tick (so a break = exceeding prior)
        while self._win and ts - self._win[0][0] > self.window_sec:
            self._win.popleft()
        prev_hi = max((p for _, p in self._win), default=None)
        prev_lo = min((p for _, p in self._win), default=None)
        n_before = len(self._win)
        self._win.append((ts, mid))

        if n_before < self.min_window_pts or prev_hi is None:
            return []
        if any(p.get("symbol") == self.symbol for p in open_positions):
            return []
        if self._last_signal_ts and (tick.ts - self._last_signal_ts).total_seconds() < self.cooldown_sec:
            return []
        if tick.ts.hour not in self.active_hours:        # HARD session gate
            return []

        rng_bps = (prev_hi - prev_lo) / prev_lo * 10000.0
        if rng_bps < float(self.min_range_bps):          # HARD range gate
            return []

        if mid > prev_hi:
            side = "buy"
        elif mid < prev_lo:
            side = "sell"
        else:
            return []

        spread_bps = float((tick.ask - tick.bid) / tick.mid * Decimal("10000"))
        edge = rng_bps * float(self.edge_frac) - spread_bps / 2.0
        urgency = "taker"                                # a breakout crosses to get in

        intent = SignalIntent(
            symbol=self.symbol, side=side,
            stop_distance_bps=self.stop_bps, take_profit_bps=self.tp_bps,
            urgency=urgency, edge_estimate_bps=Decimal(str(round(edge, 2))),
            expected_holding_seconds=self.max_holding_sec,
            signal_id=make_signal_id("brk", self.symbol, tick.ts, side),
            ts=tick.ts,
            meta={"range_bps": round(rng_bps, 1), "prev_hi": prev_hi, "prev_lo": prev_lo,
                  "engine": self.engine_id},
        )
        self._last_signal_ts = tick.ts
        return [intent]
