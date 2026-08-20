"""Breakout Continuity: range break -> shallow pullback -> hold -> continuation.

The complement to the structure bounce.  Where the bounce fades a level, this
arms only AFTER a level has broken with expansion, and takes the retest of
broken resistance as new support (mirrored for downside breaks).

Four-step sequence, all on closed bars:

1. BREAKOUT   a close beyond the highest high / lowest low of the last
              ``breakout_lookback`` bars, with ATR expansion against the prior
              20 bars and volume on the impulse bar;
2. PULLBACK   price returns toward the broken level within
              ``retest_window`` bars, no deeper than ``max_depth_atr`` past it
              -- a deep pullback is a failed break, not a retest;
3. HOLD       a rejection wick off the retest that closes back on the
              breakout side;
4. CONFIRM    the current bar closes in the breakout direction.

Direction comes from the break, so ``side_hint`` is set and the trigger
anchors on the broken level rather than deriving a side.

The breakout level is retained across bars: the impulse and the retest are
usually several bars apart, and re-deriving the level each bar would let a
later, lower high masquerade as the broken one.

RESEARCH_ONLY.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Sequence

from vnedge.execution.trigger_engine import ArmState
from vnedge.strategy.arm_sources import Bar, BarContext


@dataclass
class BreakoutState:
    """A break that is live until it is retested, expires, or is invalidated."""

    side: str
    level: float
    bar_index: int
    impulse_volume_mult: float
    atr_expansion: float
    retested: bool = False
    retest_index: int | None = None


@dataclass
class BreakoutContinuityArmSource:
    """Arm on the continuation leg of a confirmed breakout."""

    name: str = "breakout_continuity"
    breakout_lookback: int = 64
    atr_expansion_mult: float = 1.2
    impulse_volume_mult: float = 1.8
    retest_window: int = 24
    max_depth_atr: float = 0.6
    wick_frac: float = 0.30
    min_confidence: int = 70
    breakout_expiry_bars: int = 48
    # A break is void if price closes back through the level by this much ATR:
    # that is a failed breakout, and continuation is the wrong trade on it.
    invalidate_atr: float = 0.5

    _state: BreakoutState | None = field(default=None, repr=False)
    _episode: int = field(default=0, repr=False)
    last_confidence: int = field(default=0, repr=False)
    last_reason: str = field(default="", repr=False)
    last_armed: str = field(default="breakout_continuity", repr=False)

    @property
    def warmup_bars(self) -> int:
        return self.breakout_lookback + 24

    def _atr_expansion(self, ctx: BarContext) -> float:
        """The impulse bar's own range against the preceding 20 bars' mean.

        ``ctx.atr`` is deliberately NOT used as the numerator: the session
        computes it over ``bars[i-period:i]``, excluding the current bar, so a
        breakout bar's range is absent from it and expansion could never be
        detected on the bar that produced it.  Bar ``i`` is closed, so reading
        its range here is causal.
        """
        bars, i = ctx.bars, ctx.index
        if i < 40:
            return 0.0
        current = max(
            bars[i][2] - bars[i][3],
            abs(bars[i][2] - bars[i - 1][4]),
            abs(bars[i][3] - bars[i - 1][4]),
        )
        prior = [
            max(
                bars[j][2] - bars[j][3],
                abs(bars[j][2] - bars[j - 1][4]),
                abs(bars[j][3] - bars[j - 1][4]),
            )
            for j in range(i - 20, i)
        ]
        base = statistics.fmean(prior)
        return current / base if base > 0 else 0.0

    def _detect_break(self, ctx: BarContext) -> BreakoutState | None:
        bars, i = ctx.bars, ctx.index
        window = bars[i - self.breakout_lookback : i]
        if len(window) < self.breakout_lookback:
            return None
        close = bars[i][4]
        top = max(b[2] for b in window)
        bottom = min(b[3] for b in window)
        expansion = self._atr_expansion(ctx)
        if expansion < self.atr_expansion_mult:
            return None
        vol_mult = bars[i][5] / ctx.vol_ma if ctx.vol_ma > 0 else 0.0
        if vol_mult < self.impulse_volume_mult:
            return None
        if close > top:
            return BreakoutState("long", top, i, vol_mult, expansion)
        if close < bottom:
            return BreakoutState("short", bottom, i, vol_mult, expansion)
        return None

    def observe(self, ctx: BarContext) -> ArmState | None:
        if ctx.index < self.warmup_bars or ctx.atr <= 0 or ctx.vol_ma <= 0:
            return None
        bars, i = ctx.bars, ctx.index
        high, low, close = bars[i][2], bars[i][3], bars[i][4]

        state = self._state
        if state is not None:
            beyond = (
                close < state.level - self.invalidate_atr * ctx.atr
                if state.side == "long"
                else close > state.level + self.invalidate_atr * ctx.atr
            )
            if beyond or i - state.bar_index > self.breakout_expiry_bars:
                self._state = state = None  # failed break, or stale

        if state is None:
            found = self._detect_break(ctx)
            if found is not None:
                self._state = found
            return None  # never arm on the impulse bar itself

        # step 2: pullback toward the level, not through it
        depth = (
            state.level - low if state.side == "long" else high - state.level
        )
        touched = depth >= 0
        shallow = depth <= self.max_depth_atr * ctx.atr
        if touched and shallow and not state.retested:
            state.retested = True
            state.retest_index = i

        if not state.retested or state.retest_index is None:
            return None
        if i - state.retest_index > self.retest_window:
            return None

        # step 3: hold -- a rejection wick off the retest bar
        r = bars[state.retest_index]
        span = r[2] - r[3]
        if span <= 0:
            return None
        wick = (
            min(r[1], r[4]) - r[3] if state.side == "long" else r[2] - max(r[1], r[4])
        )
        if wick <= span * self.wick_frac:
            return None

        # step 4: confirmation close in the breakout direction, back on side
        if state.side == "long" and not (close > bars[i][1] and close > state.level):
            return None
        if state.side == "short" and not (close < bars[i][1] and close < state.level):
            return None

        score = 40
        if state.impulse_volume_mult >= 2.5:
            score += 15
        elif state.impulse_volume_mult >= self.impulse_volume_mult:
            score += 10
        if state.atr_expansion >= 1.5:
            score += 15
        elif state.atr_expansion >= self.atr_expansion_mult:
            score += 10
        shallowness = depth / (self.max_depth_atr * ctx.atr) if ctx.atr > 0 else 1.0
        if shallowness <= 0.34:
            score += 15
        elif shallowness <= 0.67:
            score += 10
        else:
            score += 5
        if wick > span * 0.5:
            score += 10
        if i == state.retest_index:
            score += 5  # hold and confirmation on the same bar: no drift
        confidence = max(0, min(score, 100))
        self.last_confidence = confidence
        if confidence < self.min_confidence:
            return None

        self._episode += 1
        self.last_reason = (
            f"breakout_continuity {state.side} conf={confidence} "
            f"vol={state.impulse_volume_mult:.1f}x exp={state.atr_expansion:.2f} "
            f"depth={shallowness:.2f}atr bars_since_break={i - state.bar_index}"
        )
        self._state = None  # one continuation trade per break
        edge = self.max_depth_atr * ctx.atr
        return ArmState(
            episode_id=self._episode,
            box_high=state.level + edge if state.side == "short" else state.level,
            box_low=state.level - edge if state.side == "long" else state.level,
            compressed=True,
            atr=ctx.atr,
            vol_ma=ctx.vol_ma,
            prev_close=ctx.prev_close,
            side_hint=state.side,
        )
