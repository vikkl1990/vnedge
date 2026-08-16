"""Current-hour breakout engine — catch the hourly range EXPANSION while forming.

The alternative to the previous-hour engine: instead of waiting for a completed
hour, break the running high/low built SO FAR inside the current hour — but only
after a minimum elapsed time AND a minimum range have developed (else it degenerates
into 1s noise). More aggressive / earlier / higher-frequency than the prev-hour
engine, and more prone to false breaks. One attempt per hour.

Correctness: the break is tested against the running high/low from PRIOR ticks this
hour (before this tick is folded in) — otherwise the current tick IS the new extreme
and nothing can ever break. SCAFFOLD; realized drift is the judge.
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


class CurrentHourBreakoutEngine(SignalEngine):
    engine_id = "CurrentHourBreakoutEngine"

    def __init__(
        self,
        symbol: str = "ETHUSDT",
        min_elapsed_min: float = 15.0,               # hour must be this old (range meaningful)
        min_range_bps: Decimal = Decimal("40.0"),    # range built so far
        break_buffer_bps: Decimal = Decimal("3.0"),
        min_edge_bps: Decimal = Decimal("12.0"),
        stop_buffer_bps: Decimal = Decimal("8.0"),
        target_rr: Decimal = Decimal("1.3"),
        active_hours: Optional[Sequence[int]] = (12, 13, 14, 15, 16, 17),
        max_holding_sec: int = 1800,                 # rest of the hour-ish
        edge_frac: Decimal = Decimal("0.40"),
    ):
        self.symbol = symbol
        self.min_elapsed_min = min_elapsed_min
        self.min_range_bps = min_range_bps
        self.break_buffer_bps = break_buffer_bps
        self.min_edge_bps = min_edge_bps
        self.stop_buffer_bps = stop_buffer_bps
        self.target_rr = target_rr
        self.active_hours = set(active_hours) if active_hours is not None else None
        self.max_holding_sec = max_holding_sec
        self.edge_frac = edge_frac
        self._cur: Optional[_HourState] = None
        self._broken_this_hour = False
        self._last_signal_ts: Optional[datetime] = None

    def generate(
        self,
        tick: TickSnapshot,
        account_equity: Decimal,
        open_positions: Sequence[Dict[str, Any]],
    ) -> Sequence[SignalIntent]:
        if tick.symbol != self.symbol:
            return []
        bucket = tick.ts.replace(minute=0, second=0, microsecond=0)
        price = tick.mid if tick.mid is not None else tick.last_price
        if self._cur is None or self._cur.hour_start != bucket:
            self._cur = _HourState(hour_start=bucket, high=price, low=price, open=price)
            self._broken_this_hour = False
            return []                                   # first tick of the hour: no range yet

        h = self._cur
        run_hi, run_lo, op = h.high, h.low, h.open      # range BEFORE folding in this tick
        h.high = max(h.high, price)                     # then update for next tick
        h.low = min(h.low, price)

        if any(p.get("symbol") == self.symbol for p in open_positions):
            return []
        if self.active_hours is not None and tick.ts.hour not in self.active_hours:
            return []
        if self._broken_this_hour:
            return []
        if (tick.ts - h.hour_start).total_seconds() / 60.0 < self.min_elapsed_min:
            return []
        range_bps = (run_hi - run_lo) / op * _BPS
        if range_bps < self.min_range_bps:
            return []

        buf = self.break_buffer_bps / _BPS
        if price >= run_hi * (1 + buf):
            side, broken = "buy", run_hi
        elif price <= run_lo * (1 - buf):
            side, broken = "sell", run_lo
        else:
            return []

        spread_bps = (tick.ask - tick.bid) / tick.mid * _BPS
        edge = range_bps * self.edge_frac - spread_bps / 2
        if edge < self.min_edge_bps:
            return []
        stop_dist = self.stop_buffer_bps + self.break_buffer_bps / 2
        target_bps = stop_dist * self.target_rr

        intent = SignalIntent(
            symbol=self.symbol, side=side,
            stop_distance_bps=stop_dist.quantize(Decimal("0.01")),
            take_profit_bps=target_bps.quantize(Decimal("0.01")),
            urgency="taker", edge_estimate_bps=edge.quantize(Decimal("0.01")),
            expected_holding_seconds=self.max_holding_sec,
            signal_id=make_signal_id("chb", self.symbol, tick.ts, side), ts=tick.ts,
            meta={"engine": self.engine_id, "range_bps": str(range_bps),
                  "broken_level": str(broken)},
        )
        self._broken_this_hour = True
        self._last_signal_ts = tick.ts
        return [intent]
