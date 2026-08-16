"""Short-term mean-reversion HF engine — micro-scalping's natural fit.

The bet: price has temporarily moved too far from a short-term fair value (a fast
EMA of the mid) and is more likely to snap back than continue over the next few
seconds. Guarded by a ranging-regime filter (Kaufman efficiency ratio) — a trend
is the classic killer of mean reversion, so it is a HARD block.

SCAFFOLD, not a validated edge — promotion still requires the pre-registered
≥500-paper-trade discipline. Deterministic under ordered tick replay: the EMA, the
ER window, and the last-signal timestamp all evolve with the tick SEQUENCE. Two
correctness properties: (1) ``signal_id`` is a deterministic hash (idempotency, not
random); (2) the streaming EMA + ER update on EVERY tick — never gated behind the
position/cooldown checks — so the fair value and regime never go stale or gappy
while a position is held.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional, Sequence

from vnedge.strategy.hf_regime import RangingRegimeFilter
from vnedge.strategy.signal_engine import (
    OrderFlowImbalanceEngine,
    SignalEngine,
    SignalIntent,
    TickSnapshot,
    make_signal_id,
)


class ShortTermMeanReversionEngine(SignalEngine):
    """Micro mean-reversion + mandatory ranging-regime filter."""

    engine_id = "ShortTermMeanReversionEngine"

    def __init__(
        self,
        deviation_threshold_bps: Decimal = Decimal("10.0"),
        min_edge_bps: Decimal = Decimal("7.0"),
        stop_bps: Decimal = Decimal("14.0"),
        tp_bps: Decimal = Decimal("12.0"),
        max_holding_sec: int = 40,
        symbol: str = "BTCUSDT",
        ema_span: int = 12,
        er_lookback: int = 20,
        er_threshold: Decimal = Decimal("0.28"),
    ):
        self.deviation_threshold_bps = deviation_threshold_bps
        self.min_edge_bps = min_edge_bps
        self.stop_bps = stop_bps
        self.tp_bps = tp_bps
        self.max_holding_sec = max_holding_sec
        self.symbol = symbol
        self.ema_span = ema_span
        self.regime_filter = RangingRegimeFilter(lookback=er_lookback, er_threshold=er_threshold)
        self._ema: Optional[Decimal] = None
        self._last_signal_ts: Optional[datetime] = None

    def _update_ema(self, price: Decimal) -> Decimal:
        alpha = Decimal(2) / (self.ema_span + 1)
        self._ema = price if self._ema is None else alpha * price + (Decimal(1) - alpha) * self._ema
        return self._ema

    def generate(
        self,
        tick: TickSnapshot,
        account_equity: Decimal,
        open_positions: Sequence[Dict[str, Any]],
    ) -> Sequence[SignalIntent]:
        if tick.symbol != self.symbol:
            return []

        mid = tick.mid if tick.mid is not None else (tick.bid + tick.ask) / 2
        # Streaming state updates on EVERY tick (never gated) — else the EMA goes
        # stale and the ER window goes gappy while a position is held / in cooldown.
        ema = self._update_ema(mid)
        regime = self.regime_filter.update(mid)

        # --- emission gates (state already advanced above) ---
        if any(p.get("symbol") == self.symbol for p in open_positions):
            return []                                   # flat-only v1
        if self._last_signal_ts and (tick.ts - self._last_signal_ts).total_seconds() < 4.0:
            return []                                   # short cooldown
        if not regime.is_ranging:
            return []                                   # HARD block in a trend

        deviation_bps = ((mid - ema) / ema) * Decimal("10000")
        side: Optional[str] = None
        edge = Decimal("0")
        if deviation_bps >= self.deviation_threshold_bps:
            side, edge = "sell", deviation_bps - (self.deviation_threshold_bps / 2)
        elif deviation_bps <= -self.deviation_threshold_bps:
            side, edge = "buy", abs(deviation_bps) - (self.deviation_threshold_bps / 2)
        if side is None or edge < self.min_edge_bps:
            return []

        spread_bps = ((tick.ask - tick.bid) / mid) * Decimal("10000")
        urgency = ("maker" if spread_bps <= Decimal("4.0") and edge < Decimal("15")
                   else "taker")

        intent = SignalIntent(
            symbol=tick.symbol, side=side,
            stop_distance_bps=self.stop_bps, take_profit_bps=self.tp_bps,
            urgency=urgency, edge_estimate_bps=edge.quantize(Decimal("0.01")),
            expected_holding_seconds=self.max_holding_sec,
            signal_id=make_signal_id("mr", tick.symbol, tick.ts, side),
            ts=tick.ts,
            meta={"deviation_bps": str(deviation_bps), "ema": str(ema),
                  "efficiency_ratio": str(regime.efficiency_ratio), "regime": regime.reason,
                  "spread_bps": str(spread_bps), "engine": self.engine_id},
        )
        self._last_signal_ts = tick.ts
        return [intent]


def build_hf_engines(
    symbols: Sequence[str] = ("BTCUSDT",),
    *,
    include_order_flow: bool = True,
    include_mean_reversion: bool = True,
) -> list[SignalEngine]:
    """Production HF engine roster: order-flow + mean-reversion per symbol
    (1–3 total per the contract). Both feed the same CostGate."""
    engines: list[SignalEngine] = []
    for sym in symbols:
        if include_order_flow:
            engines.append(OrderFlowImbalanceEngine(symbol=sym))
        if include_mean_reversion:
            engines.append(ShortTermMeanReversionEngine(symbol=sym))
    return engines
