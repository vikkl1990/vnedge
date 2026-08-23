"""ema_vwap_cross_v1 — frozen to docs/prereg/ema_vwap_cross_v1_20260823.md.

The side comes from where price sits against the VOLUME-WEIGHTED average other
participants paid, not against structure price built itself. That is the whole
reason the ID exists: three closed families all read structure, and all failed
on gross before costs.

Sequence, on closed 1h bars:

1. VWAP     rolling 24h volume-weighted average, advanced every bar.
2. CROSS    EMA(9) crosses it -- above for long, below for short.
3. ENTER    at the close of the crossing bar.

Deliberately NO gates. No regime filter, expansion band, oscillator veto,
session window or confluence requirement. Each of those was added to an earlier
family and none of them changed a gross edge that was not there; a mechanism
that needs one to show a signal does not have one.

The stop is 2.0 ATR, encoded through the ArmState box span exactly as the other
pre-registered arms do, so the trigger keeps owning the single stop rule.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Sequence

from vnedge.execution.trigger_engine import ArmState
from vnedge.strategy.arm_sources import Bar, BarContext


@dataclass
class EmaVwapCrossArmSource:
    """Frozen pre-registered EMA-vs-VWAP cross arm."""

    name: str = "ema_vwap_cross_v1"
    ema_period: int = 9
    vwap_bars: int = 24          # rolling 24h on 1h bars
    atr_bars: int = 14
    stop_atr_mult: float = 2.0

    _ema: float | None = field(default=None, repr=False)
    _above: bool | None = field(default=None, repr=False)
    _episode: int = field(default=0, repr=False)
    trend_side: str | None = field(default=None, repr=False)
    last_confidence: int = field(default=100, repr=False)
    last_reason: str = field(default="", repr=False)
    last_armed: str = field(default="ema_vwap_cross_v1", repr=False)

    @property
    def warmup_bars(self) -> int:
        return max(self.vwap_bars, self.ema_period, self.atr_bars) + 2

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
        close, volume = bars[i][4], bars[i][5]

        # The EMA advances on every observed bar; the VWAP is computed from the
        # window directly rather than accumulated incrementally. An incremental
        # sum assumes the arm sees EVERY bar from index 0, and ScannerSession
        # does not call observe() until its own feature warmup completes -- so
        # the first subtraction removed volume that was never added, corrupting
        # the denominator and silencing the arm for long stretches. Measured:
        # 16 arms inside a session against 697 standalone on the same bars.
        k = 2.0 / (self.ema_period + 1)
        self._ema = close if self._ema is None else self._ema + k * (close - self._ema)
        if i < self.warmup_bars:
            return None
        window = bars[i - self.vwap_bars + 1 : i + 1]
        vv = sum(b[5] for b in window)
        if vv <= 0:
            return None
        vwap = sum(b[4] * b[5] for b in window) / vv

        above = self._ema > vwap
        prior, self._above = self._above, above
        if prior is None or above == prior:
            self.trend_side = "long" if above else "short"
            return None                       # no cross on this bar

        side = "long" if above else "short"
        self.trend_side = side
        atr = self._atr(bars, i)
        if atr <= 0:
            return None

        span = self.stop_atr_mult * atr
        if side == "long":
            box_low, box_high = close, close + span
        else:
            box_high, box_low = close, close - span

        self._episode += 1
        self.last_reason = (
            f"ema_vwap_cross_v1 {side} ema{self.ema_period}={self._ema:.4f} "
            f"vwap{self.vwap_bars}h={vwap:.4f} stop_span={span:.4f}"
        )
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
