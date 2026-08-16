"""HF ranging-regime filter — the guard that keeps mean-reversion out of trends.

A streaming Kaufman Efficiency Ratio over the last N tick/1s prices:

    ER = |net move| / sum(|per-step moves|)

Low ER → price is chopping (ranging) → mean-reversion is allowed. High ER →
directional efficiency (a trend) → mean-reversion is HARD-BLOCKED. This is the
single most important guard for a pure mean-reversion edge: a trending regime is
its classic killer. Deterministic under ordered replay (its only state is the
price deque). Kept SEPARATE from the candle-based ``strategy.regime`` baseline —
this one runs on the tick hot path, that one on candle DataFrames.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Deque

from pydantic import BaseModel


class RegimeState(BaseModel):
    model_config = {"frozen": True}

    is_ranging: bool
    efficiency_ratio: Decimal
    reason: str            # "ranging" | "trending" | "warming_up"


class RangingRegimeFilter:
    """Streaming efficiency-ratio filter. Locked knobs: lookback, er_threshold."""

    def __init__(
        self,
        lookback: int = 20,                        # ~20s on 1s bars
        er_threshold: Decimal = Decimal("0.28"),   # ER <= this ⇒ ranging
    ):
        self.lookback = lookback
        self.er_threshold = er_threshold
        self._prices: Deque[Decimal] = deque(maxlen=lookback + 1)

    def update(self, price: Decimal) -> RegimeState:
        self._prices.append(price)
        if len(self._prices) < self.lookback + 1:
            return RegimeState(is_ranging=False, efficiency_ratio=Decimal("1.0"),
                               reason="warming_up")
        prices = list(self._prices)
        net_move = abs(prices[-1] - prices[0])
        path = sum(abs(prices[i] - prices[i - 1]) for i in range(1, len(prices)))
        er = Decimal("0") if path == 0 else (net_move / path).quantize(Decimal("0.0001"))
        ranging = er <= self.er_threshold
        return RegimeState(is_ranging=ranging, efficiency_ratio=er,
                           reason="ranging" if ranging else "trending")

    def reset(self) -> None:
        self._prices.clear()
