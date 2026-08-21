"""breakout_continuity_v1 — frozen to docs/prereg/breakout_continuity_v1_20260821.md.

Distinct from ``breakout_continuity.BreakoutContinuityArmSource``, which was an
un-pre-registered 5m sweep (burned 2026-05-21 -> 2026-08-19, registry
fa15bc66). This module implements the pre-registered specification and nothing
else. Parameters here are FROZEN: changing one requires a new pre-registration
ID, not an edit.

Sequence, all on closed 15m bars:

1. BREAK     a close beyond the Donchian-20 extreme of the prior 20 closed
             bars by at least ``break_atr_margin`` x ATR(14). Wick-only
             excursions do not count.
2. PULLBACK  within ``pullback_window`` bars price returns toward the broken
             level, no deeper than the invalidation distance.
3. RECLAIM   a close back in the break direction beyond the level.

EXIT WIRING NOTE. The pre-registration says invalidation is a 15m close DEEP
through the level. The shared exit engine's ``failed_breakout`` rule fires on
ANY close back through the box edge, which is a much stricter rule and not what
was specified: measured on the selection window it closed 33% of trades at
-21.62 bps within half an hour. Callers must therefore run this arm with
``failed_breakout=False`` and let the level-anchored stop serve as the
invalidation, which is what the 0.2 ATR distance is for.

The stop is 0.2 ATR beyond the pullback extreme. It is encoded through the
ArmState box edges because the trigger derives its stop distance from
``abs(box_high - box_low)`` in zone mode: for a long the level sits at
``box_low`` and the stop lands at ``box_low - (box_high - box_low)``, so the
span is set to exactly the distance wanted. That keeps one stop rule in the
trigger rather than a second one here.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Sequence

from vnedge.execution.trigger_engine import ArmState
from vnedge.strategy.arm_sources import Bar, BarContext


@dataclass
class _Break:
    side: str
    level: float
    bar_index: int
    atr: float
    pulled_back: bool = False
    pullback_extreme: float | None = None
    pullback_index: int | None = None


@dataclass
class BreakoutContinuityV1ArmSource:
    """Frozen pre-registered breakout-continuation arm."""

    name: str = "breakout_continuity_v1"
    donchian_bars: int = 20
    atr_bars: int = 14
    break_atr_margin: float = 0.05
    pullback_window: int = 12
    invalidate_atr: float = 1.0     # a pullback deeper than this voids the break
    stop_atr_beyond: float = 0.2
    expiry_bars: int = 48

    _state: _Break | None = field(default=None, repr=False)
    _episode: int = field(default=0, repr=False)
    last_confidence: int = field(default=100, repr=False)
    last_reason: str = field(default="", repr=False)
    last_armed: str = field(default="breakout_continuity_v1", repr=False)

    @property
    def warmup_bars(self) -> int:
        return self.donchian_bars + self.atr_bars + 2

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
        if i < self.warmup_bars:
            return None
        atr = self._atr(bars, i)
        if atr <= 0:
            return None
        high, low, close = bars[i][2], bars[i][3], bars[i][4]

        state = self._state
        if state is not None:
            stale = i - state.bar_index > self.expiry_bars
            through = (
                close < state.level - self.invalidate_atr * state.atr
                if state.side == "long"
                else close > state.level + self.invalidate_atr * state.atr
            )
            if stale or through:
                self._state = state = None

        if state is None:
            window = bars[i - self.donchian_bars : i]
            if len(window) < self.donchian_bars:
                return None
            margin = self.break_atr_margin * atr
            top = max(b[2] for b in window)
            bottom = min(b[3] for b in window)
            if close > top + margin:
                self._state = _Break("long", top, i, atr)
            elif close < bottom - margin:
                self._state = _Break("short", bottom, i, atr)
            return None            # never arm on the break bar itself

        # step 2: pullback toward the level
        if not state.pulled_back:
            reached = low <= state.level if state.side == "long" else high >= state.level
            if reached:
                state.pulled_back = True
                state.pullback_extreme = low if state.side == "long" else high
                state.pullback_index = i
            return None
        assert state.pullback_extreme is not None and state.pullback_index is not None
        # track a deeper pullback while still inside the window
        if state.side == "long":
            state.pullback_extreme = min(state.pullback_extreme, low)
        else:
            state.pullback_extreme = max(state.pullback_extreme, high)
        if i - state.pullback_index > self.pullback_window:
            self._state = None
            return None

        # step 3: reclaim
        reclaimed = (
            close > state.level if state.side == "long" else close < state.level
        )
        if not reclaimed:
            return None

        if state.side == "long":
            span = state.level - state.pullback_extreme + self.stop_atr_beyond * atr
            box_low, box_high = state.level, state.level + span
        else:
            span = state.pullback_extreme - state.level + self.stop_atr_beyond * atr
            box_high, box_low = state.level, state.level - span
        if span <= 0:
            self._state = None
            return None

        self._episode += 1
        self.last_reason = (
            f"breakout_continuity_v1 {state.side} level={state.level:.2f} "
            f"pullback={state.pullback_extreme:.2f} stop_span={span:.2f} "
            f"bars_since_break={i - state.bar_index}"
        )
        self._state = None             # one continuation per break
        return ArmState(
            episode_id=self._episode,
            box_high=box_high,
            box_low=box_low,
            compressed=True,
            atr=atr,
            vol_ma=ctx.vol_ma,
            prev_close=ctx.prev_close,
            side_hint=state.side,
        )
