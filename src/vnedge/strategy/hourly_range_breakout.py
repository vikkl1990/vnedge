"""Hourly-range breakout engine — break the PREVIOUS COMPLETED HOUR's high/low.

Targets the real movement size measured on ETH (~67bps median hourly range) instead
of 1s noise: track the current hour's high/low, finalize it on the hour rollover,
and on the next hour take a break of that prior hour's extreme (± a buffer), gated
to the active session and a wide-enough prior range. Edge is derived from the SIZE
of the broken range (a real, measured number the CostGate can honestly judge), not a
magic formula. One breakout attempt per hour.

SCAFFOLD — a signal that clears the CostGate is not a validated edge; realized drift
(profit ladder / horizon) is the judge. Deterministic under ordered replay. This is
the reconstructed, corrected form of the user's HourlyRangeBreakoutEngine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional, Sequence

from vnedge.strategy.signal_engine import (
    SignalEngine,
    SignalIntent,
    TickSnapshot,
    make_signal_id,
)

_BPS = Decimal("10000")


@dataclass
class _HourState:
    hour_start: datetime
    high: Decimal
    low: Decimal
    open: Decimal
    close: Decimal
    range_bps: Decimal = Decimal("0")
    finalized: bool = False


class HourlyRangeBreakoutEngine(SignalEngine):
    engine_id = "HourlyRangeBreakoutEngine"

    def __init__(
        self,
        symbol: str = "ETHUSDT",
        min_range_bps: Decimal = Decimal("55.0"),      # prior hour must be wide enough
        break_buffer_bps: Decimal = Decimal("3.0"),    # confirm the break past the level
        min_edge_bps: Decimal = Decimal("12.0"),
        stop_buffer_bps: Decimal = Decimal("8.0"),     # stop sits beyond the broken level
        target_rr: Decimal = Decimal("1.3"),           # target = RR × stop distance
        active_hours: Optional[Sequence[int]] = (12, 13, 14, 15, 16, 17),
        max_holding_sec: int = 3600,
        edge_frac: Decimal = Decimal("0.45"),          # edge ≈ 45% of the prior range
    ):
        self.symbol = symbol
        self.min_range_bps = min_range_bps
        self.break_buffer_bps = break_buffer_bps
        self.min_edge_bps = min_edge_bps
        self.stop_buffer_bps = stop_buffer_bps
        self.target_rr = target_rr
        self.active_hours = set(active_hours) if active_hours is not None else None
        self.max_holding_sec = max_holding_sec
        self.edge_frac = edge_frac
        self._current: Optional[_HourState] = None
        self._prev: Optional[_HourState] = None
        self._last_signal_ts: Optional[datetime] = None
        self._broken_this_hour = False

    @staticmethod
    def _bucket(ts: datetime) -> datetime:
        return ts.replace(minute=0, second=0, microsecond=0)

    def _update_hour_state(self, tick: TickSnapshot) -> None:
        bucket = self._bucket(tick.ts)
        price = tick.mid if tick.mid is not None else tick.last_price
        if self._current is None or self._current.hour_start != bucket:
            if self._current is not None:                       # finalize the completed hour
                h = self._current
                h.close = price
                h.range_bps = (h.high - h.low) / h.open * _BPS
                h.finalized = True
                self._prev = h
                self._broken_this_hour = False
            self._current = _HourState(hour_start=bucket, high=price, low=price,
                                       open=price, close=price)
        else:
            h = self._current
            h.high = max(h.high, price)
            h.low = min(h.low, price)
            h.close = price

    def generate(
        self,
        tick: TickSnapshot,
        account_equity: Decimal,
        open_positions: Sequence[Dict[str, Any]],
    ) -> Sequence[SignalIntent]:
        if tick.symbol != self.symbol:
            return []
        self._update_hour_state(tick)                            # ALWAYS update state first

        if any(p.get("symbol") == self.symbol for p in open_positions):
            return []
        if self.active_hours is not None and tick.ts.hour not in self.active_hours:
            return []
        prev = self._prev
        if prev is None or not prev.finalized or self._broken_this_hour:
            return []
        if prev.range_bps < self.min_range_bps:
            return []

        price = tick.mid if tick.mid is not None else tick.last_price
        buf = self.break_buffer_bps / _BPS
        if price >= prev.high * (1 + buf):
            side, broken = "buy", prev.high
            stop_price = broken * (1 - self.stop_buffer_bps / _BPS)
            stop_dist = (price - stop_price) / price * _BPS
        elif price <= prev.low * (1 - buf):
            side, broken = "sell", prev.low
            stop_price = broken * (1 + self.stop_buffer_bps / _BPS)
            stop_dist = (stop_price - price) / price * _BPS
        else:
            return []

        target_bps = self.target_rr * stop_dist
        spread_bps = (tick.ask - tick.bid) / tick.mid * _BPS
        edge = prev.range_bps * self.edge_frac - spread_bps / 2
        if edge < self.min_edge_bps:
            return []

        intent = SignalIntent(
            symbol=self.symbol, side=side,
            stop_distance_bps=stop_dist.quantize(Decimal("0.01")),
            take_profit_bps=target_bps.quantize(Decimal("0.01")),
            urgency="taker", edge_estimate_bps=edge.quantize(Decimal("0.01")),
            expected_holding_seconds=self.max_holding_sec,
            signal_id=make_signal_id("hrb", self.symbol, tick.ts, side), ts=tick.ts,
            meta={"engine": self.engine_id, "prev_range_bps": str(prev.range_bps),
                  "broken_level": str(broken), "prev_hour": prev.hour_start.isoformat()},
        )
        self._broken_this_hour = True
        self._last_signal_ts = tick.ts
        return [intent]
