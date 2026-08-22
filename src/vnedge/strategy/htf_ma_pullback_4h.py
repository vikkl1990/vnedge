"""htf_ma_pullback_4h_v1 — frozen to docs/prereg/htf_ma_pullback_4h_v1_20260821.md.

The side comes from 4h trend state, never from the entry bar's own shape. That
is the whole point of the ID: the two closed families both derived side from a
local structure event and both failed on gross edge, so here the local event
only decides TIMING inside a direction already fixed by the higher timeframe.

Sequence, all on closed 4h bars:

1. TREND     EMA20 > EMA50 -> long only; EMA20 < EMA50 -> short only; else flat.
2. PULLBACK  price trades back to touch EMA20 against the trend.
3. RECLAIM   a 4h close back on the trend side of EMA20.

The stop is 2.0 ATR beyond the pullback extreme, encoded through the ArmState
box span exactly as ``breakout_continuity_v1`` does, so the trigger keeps
owning the one stop rule.

``trend_side`` is exposed so a runner can honour the specified EMA-cross exit,
which the shared exit engine has no concept of.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Sequence

from vnedge.execution.trigger_engine import ArmState
from vnedge.strategy.arm_sources import Bar, BarContext


@dataclass
class HtfMaPullback4hArmSource:
    """Frozen pre-registered 4h trend-pullback arm."""

    name: str = "htf_ma_pullback_4h_v1"
    fast_ema: int = 20
    slow_ema: int = 50
    atr_bars: int = 14
    stop_atr_mult: float = 2.0
    #: A pullback must resolve within this many bars or the setup is dropped.
    pullback_window: int = 12

    _fast: float | None = field(default=None, repr=False)
    _slow: float | None = field(default=None, repr=False)
    _seeded: int = field(default=0, repr=False)
    _pulled_back: bool = field(default=False, repr=False)
    _pullback_extreme: float | None = field(default=None, repr=False)
    _pullback_index: int = field(default=-10**9, repr=False)
    _episode: int = field(default=0, repr=False)
    trend_side: str | None = field(default=None, repr=False)
    last_confidence: int = field(default=100, repr=False)
    last_reason: str = field(default="", repr=False)
    last_armed: str = field(default="htf_ma_pullback_4h_v1", repr=False)

    @property
    def warmup_bars(self) -> int:
        return self.slow_ema + self.atr_bars + 2

    def _atr(self, bars: Sequence[Bar], i: int) -> float:
        n = self.atr_bars
        if i < n + 1:
            return 0.0
        return statistics.fmean(
            max(
                bars[j][2] - bars[j][3],
                abs(bars[j][2] - bars[j - 1][4]),
                abs(bars[j][3] - bars[j - 1][4]),
            )
            for j in range(i - n, i)
        )

    def observe(self, ctx: BarContext) -> ArmState | None:
        bars, i = ctx.bars, ctx.index
        close = bars[i][4]

        # EMAs advance on EVERY closed bar, including while a position is open
        # and before the reporting window, or the trend state develops gaps.
        kf, ks = 2.0 / (self.fast_ema + 1), 2.0 / (self.slow_ema + 1)
        if self._fast is None:
            self._fast = self._slow = close
        else:
            self._fast += kf * (close - self._fast)
            self._slow += ks * (close - self._slow)
        self._seeded += 1
        if self._seeded <= self.warmup_bars or i < self.warmup_bars:
            return None

        side = ("long" if self._fast > self._slow
                else "short" if self._fast < self._slow else None)
        if side != self.trend_side:
            # a flip resets any half-formed setup; the old direction is void
            self._pulled_back = False
            self._pullback_extreme = None
        self.trend_side = side
        if side is None:
            return None

        atr = self._atr(bars, i)
        if atr <= 0:
            return None
        high, low = bars[i][2], bars[i][3]

        # step 2: pullback touches the fast EMA against the trend
        touched = low <= self._fast if side == "long" else high >= self._fast
        if touched:
            extreme = low if side == "long" else high
            if not self._pulled_back:
                self._pulled_back = True
                self._pullback_extreme = extreme
                self._pullback_index = i
            else:
                self._pullback_extreme = (
                    min(self._pullback_extreme, extreme) if side == "long"
                    else max(self._pullback_extreme, extreme)
                )
        if not self._pulled_back or self._pullback_extreme is None:
            return None
        if i - self._pullback_index > self.pullback_window:
            self._pulled_back = False
            self._pullback_extreme = None
            return None

        # step 3: reclaim — close back on the trend side of the fast EMA
        reclaimed = close > self._fast if side == "long" else close < self._fast
        # The touch must have happened on an EARLIER bar. Testing "and not
        # touched" instead would reject almost every real reclaim, because a
        # bar that closes back above the EMA usually still dips below it
        # intrabar -- that is what a pullback bar looks like.
        if not reclaimed or i <= self._pullback_index:
            return None

        if side == "long":
            span = close - (self._pullback_extreme - self.stop_atr_mult * atr)
            box_low, box_high = close, close + span
        else:
            span = (self._pullback_extreme + self.stop_atr_mult * atr) - close
            box_high, box_low = close, close - span
        if span <= 0:
            return None

        self._episode += 1
        self.last_reason = (
            f"htf_ma_pullback_4h_v1 {side} ema20={self._fast:.2f} "
            f"ema50={self._slow:.2f} pullback={self._pullback_extreme:.2f} "
            f"stop_span={span:.2f}"
        )
        self._pulled_back = False
        self._pullback_extreme = None
        return ArmState(
            episode_id=self._episode,
            box_high=box_high,
            box_low=box_low,
            compressed=True,
            atr=atr,
            vol_ma=ctx.vol_ma,
            prev_close=ctx.prev_close,
            side_hint=side,
        )
