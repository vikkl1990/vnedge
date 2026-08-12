"""Predictable exit / profit capture — a bps ladder with a ratcheting stop.

Stop and targets are prices derived from bps. Rules: stop wins ties; the stop
only ever tightens (moves to breakeven after the first target, then trails if
configured); partials scale out on the ladder; a hard time-stop closes the
residual. Stateful across bars for one position.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from vnedge.plan.trade_plan import TradePlan, stop_price, target_price


@dataclass(frozen=True)
class ExitEvent:
    action: Literal["partial", "close"]
    size_pct: float          # fraction of the ORIGINAL position closed by this event
    price: float
    reason: str              # "stop" | "tp1".. | "trail" | "time_stop"


class ExitEngine:
    def __init__(self, plan: TradePlan, entry_price: float) -> None:
        self.plan = plan
        self.entry = entry_price
        self.side = plan.side
        self.stop = stop_price(entry_price, plan.risk.stop_bps, self.side)
        # targets ordered nearest→farthest from entry
        tgts = [(target_price(entry_price, t.bps, self.side), t.size_pct) for t in plan.profit.targets]
        self.targets = sorted(tgts, key=lambda x: x[0], reverse=(self.side == "short"))
        self._hit = [False] * len(self.targets)
        self.remaining = 1.0
        self.closed = False
        self._extreme = entry_price

    def _tighten(self, new_stop: float) -> None:
        # ratchet only: never widen the stop
        self.stop = max(self.stop, new_stop) if self.side == "long" else min(self.stop, new_stop)

    def on_bar(self, high: float, low: float, close: float, bars_in_trade: int) -> list[ExitEvent]:
        if self.closed:
            return []
        out: list[ExitEvent] = []

        # 1. stop first (stop wins ties)
        if (low <= self.stop) if self.side == "long" else (high >= self.stop):
            out.append(ExitEvent("close", self.remaining, self.stop, "stop"))
            self.remaining = 0.0
            self.closed = True
            return out

        # 2. bps target ladder
        for i, (tp, sz) in enumerate(self.targets):
            if self._hit[i]:
                continue
            if (high >= tp) if self.side == "long" else (low <= tp):
                self._hit[i] = True
                take = min(sz / 100.0, self.remaining)
                self.remaining -= take
                closing = self.remaining <= 1e-9
                out.append(ExitEvent("close" if closing else "partial", take, tp, f"tp{i + 1}"))
                self._tighten(self.entry)   # first target → move stop to breakeven
        if self.remaining <= 1e-9:
            self.closed = True
            return out

        # 3. trail after any target hit
        if self.plan.profit.trail_bps is not None and any(self._hit):
            self._extreme = max(self._extreme, high) if self.side == "long" else min(self._extreme, low)
            self._tighten(stop_price(self._extreme, self.plan.profit.trail_bps, self.side))

        # 4. hard time-stop
        if bars_in_trade >= self.plan.profit.time_stop_bars:
            out.append(ExitEvent("close", self.remaining, close, "time_stop"))
            self.remaining = 0.0
            self.closed = True
        return out
