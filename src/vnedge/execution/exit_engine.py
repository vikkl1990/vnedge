"""Exit plane: manage discipline for squeeze-expansion positions.

RESEARCH_ONLY helper, shared verbatim by ``research/squeeze_trigger_replay``
so replay evidence and runtime behaviour cannot diverge.  The caller owns
fees and journaling; this module owns only the exit policy:

1. hard SL first (pessimistic, level-anchored by the trigger plane);
2. failed-breakout kill: a later bar closing back inside the box;
3. no-progress time stop: MFE below ``no_progress_min_r`` after the window;
4. absolute 4h backstop;
5. otherwise ratchet: breakeven-plus-fees lock after +1R, chandelier trail
   (extreme -/+ ``trail_atr_mult`` * ATR) after +2R.  No fixed take-profit:
   expansion capture keeps the right tail.

``on_bar`` is the full policy at closed-bar cadence; ``on_tick`` is the
protective stop between bars (the tick plane grows from here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Side = Literal["long", "short"]


@dataclass(frozen=True, slots=True)
class ExitConfig:
    """Frozen exit-plane parameters."""

    # OFF by default: runtime.active_exit -- the engine that actually trades --
    # has no no-progress rule, so a research book that closes on it predicts a
    # book the shadow lane will never produce. Set an int to opt in for a
    # research question, knowing the result is then not runtime-comparable.
    no_progress_bars: int | None = None
    no_progress_min_r: float = 0.5
    breakeven_arm_r: float = 1.0
    trail_arm_r: float = 2.0
    trail_atr_mult: float = 1.0
    absolute_max_bars: int = 48
    taker_bps: float = 5.9
    # Optional complete round-trip cost used by the breakeven ratchet.  Older
    # registered profiles retain their one-leg ``taker_bps`` semantics; new
    # profiles can prevent a gross breakeven stop from being a net loser.
    breakeven_cost_bps: float | None = None
    be_fee_buffer_bps: float = 1.0
    # Breakout entries sit BEYOND the box, so "closed back inside" is a clean
    # invalidation. Bounce entries sit AT the zone, where the same test fires
    # within a bar or two and pre-empts the ATR stop -- measured at 43% of
    # exits and -4977 bps on the structure-bounce arm. Profiles that enter at
    # a level turn it off and let the stop do its job.
    failed_breakout: bool = True
    # Scale-out ladder: ((R multiple, fraction), ...). Empty keeps the
    # single-exit behaviour every existing arm was measured under.
    tp_ladder: tuple[tuple[float, float], ...] = ()
    # Move the stop to breakeven once the first rung fills.
    breakeven_after_tp1: bool = True
    # Hard age cap in bars; None defers to ``absolute_max_bars``.
    max_age_bars: int | None = None

    def __post_init__(self) -> None:
        if self.no_progress_bars is not None and self.no_progress_bars < 1:
            raise ValueError("no-progress settings are invalid")
        if self.no_progress_min_r <= 0:
            raise ValueError("no-progress settings are invalid")
        if self.breakeven_arm_r <= 0 or self.trail_arm_r <= self.breakeven_arm_r - 1e-9:
            raise ValueError("ratchet thresholds are invalid")
        if self.trail_atr_mult <= 0 or self.absolute_max_bars < 1:
            raise ValueError("trail/backstop settings are invalid")
        if self.taker_bps < 0 or self.be_fee_buffer_bps < 0:
            raise ValueError("fee settings are invalid")
        if self.breakeven_cost_bps is not None and self.breakeven_cost_bps < 0:
            raise ValueError("breakeven_cost_bps cannot be negative")
        if self.max_age_bars is not None and self.max_age_bars < 1:
            raise ValueError("max_age_bars must be positive when set")
        if self.tp_ladder:
            rungs = [r for r, _ in self.tp_ladder]
            if any(r <= 0 for r in rungs) or rungs != sorted(rungs):
                raise ValueError("tp ladder R multiples must ascend and be positive")
            if any(f <= 0 for _, f in self.tp_ladder):
                raise ValueError("tp ladder fractions must be positive")
            if sum(f for _, f in self.tp_ladder) > 1.0 + 1e-9:
                raise ValueError("tp ladder fractions cannot exceed the position")


@dataclass
class ExitPosition:
    side: Side
    entry: float
    stop: float
    risk: float
    box_edge: float
    entry_bar: int
    mfe: float = 0.0
    extreme: float = 0.0
    remaining: float = 1.0
    breakeven_armed: bool = False
    # Favourable price delta already banked by filled rungs, weighted by the
    # fraction each one closed. Kept in price terms so the blended exit price
    # handed back to the caller needs no special-casing downstream.
    realized: float = 0.0
    rungs_filled: int = 0

    def __post_init__(self) -> None:
        if self.extreme == 0.0:
            self.extreme = self.entry


@dataclass(frozen=True, slots=True)
class ExitDecision:
    reason: str
    price: float
    won: bool


@dataclass
class ExitEngine:
    config: ExitConfig = field(default_factory=ExitConfig)
    pos: ExitPosition | None = None

    def open_from_fire(
        self,
        *,
        side: Side,
        entry: float,
        stop: float,
        risk: float,
        box_edge: float,
        entry_bar: int,
    ) -> None:
        if self.pos is not None:
            raise ValueError("exit engine already manages a position")
        self.pos = ExitPosition(
            side=side, entry=entry, stop=stop, risk=risk,
            box_edge=box_edge, entry_bar=entry_bar,
        )

    def clear(self) -> None:
        self.pos = None

    def on_bar(
        self,
        *,
        high: float,
        low: float,
        close: float,
        atr: float,
        bar_index: int,
    ) -> ExitDecision | None:
        p = self.pos
        if p is None:
            return None
        c = self.config
        side = p.side
        held = bar_index - p.entry_bar

        favorable = (high - p.entry) if side == "long" else (p.entry - low)
        p.mfe = max(p.mfe, favorable)
        p.extreme = max(p.extreme, high) if side == "long" else min(p.extreme, low)

        # 1) hard SL first (pessimistic within-bar ordering): a bar that
        # touches both a rung and the stop is booked as the stop, matching the
        # stop-wins-ties convention used everywhere else.
        stop_hit = low <= p.stop if side == "long" else high >= p.stop
        if stop_hit:
            # runtime.active_exit distinguishes a stop that has ratcheted to
            # breakeven from the original one; matching the name keeps exit
            # histograms mergeable across research and shadow.
            reason = "breakeven_stop" if p.breakeven_armed else "stop"
            self.clear()
            return self._close(p, p.stop, reason=reason)

        # 1b) scale-out rungs
        if c.tp_ladder:
            for level_r, fraction in c.tp_ladder[p.rungs_filled :]:
                target = (
                    p.entry + level_r * p.risk
                    if side == "long"
                    else p.entry - level_r * p.risk
                )
                reached = high >= target if side == "long" else low <= target
                if not reached:
                    break
                take = min(fraction, p.remaining)
                p.realized += take * (
                    (target - p.entry) if side == "long" else (p.entry - target)
                )
                p.remaining -= take
                p.rungs_filled += 1
                if p.rungs_filled == 1 and c.breakeven_after_tp1:
                    cost_bps = (
                        c.breakeven_cost_bps
                        if c.breakeven_cost_bps is not None
                        else c.taker_bps
                    )
                    pad = (cost_bps + c.be_fee_buffer_bps) / 10_000.0
                    be = p.entry * (1 + pad) if side == "long" else p.entry * (1 - pad)
                    tightened = max(p.stop, be) if side == "long" else min(p.stop, be)
                    if tightened != p.stop:
                        p.breakeven_armed = True
                    p.stop = tightened
                if p.remaining <= 1e-9:
                    self.clear()
                    return self._close(p, target, reason="tp_ladder")

        # 1c) hard age cap
        if c.max_age_bars is not None and held >= c.max_age_bars:
            self.clear()
            return self._close(p, close, reason="max_age")

        # 2) failed breakout: close back inside the box (from the bar after entry)
        if c.failed_breakout and held >= 1:
            back_inside = close < p.box_edge if side == "long" else close > p.box_edge
            if back_inside:
                self.clear()
                return self._close(p, close, reason="failed_breakout")

        # 3) no progress -- research-only, off unless explicitly requested
        if (c.no_progress_bars is not None
                and held >= c.no_progress_bars
                and p.mfe < c.no_progress_min_r * p.risk):
            self.clear()
            return self._close(p, close, reason="no_progress")

        # 4) absolute backstop
        if held >= c.absolute_max_bars:
            self.clear()
            return self._close(p, close, reason="max_holding")

        # 5) ratchets (no exit this bar)
        if p.mfe >= c.breakeven_arm_r * p.risk:
            cost_bps = (
                c.breakeven_cost_bps
                if c.breakeven_cost_bps is not None
                else c.taker_bps
            )
            pad = (cost_bps + c.be_fee_buffer_bps) / 10_000.0
            breakeven = p.entry * (1.0 + pad) if side == "long" else p.entry * (1.0 - pad)
            tightened = max(p.stop, breakeven) if side == "long" else min(p.stop, breakeven)
            if tightened != p.stop:
                p.breakeven_armed = True
            p.stop = tightened
        if p.mfe >= c.trail_arm_r * p.risk and atr > 0:
            trail = (
                p.extreme - c.trail_atr_mult * atr
                if side == "long"
                else p.extreme + c.trail_atr_mult * atr
            )
            p.stop = max(p.stop, trail) if side == "long" else min(p.stop, trail)
        return None

    def on_tick(self, *, price: float) -> ExitDecision | None:
        """Protective stop only between bars; the full policy runs on_bar."""
        p = self.pos
        if p is None:
            return None
        stop_hit = price <= p.stop if p.side == "long" else price >= p.stop
        if stop_hit:
            self.clear()
            return self._close(p, p.stop, reason="stop_tick")
        return None

    @staticmethod
    def _close(p: ExitPosition, price: float, *, reason: str) -> ExitDecision:
        """Blend banked rungs with the final leg into one effective exit price.

        Callers price a trade from a single exit level, so a scaled-out
        position reports the price that reproduces its true weighted PnL --
        no downstream code needs to know a ladder was used.
        """
        final = (price - p.entry) if p.side == "long" else (p.entry - price)
        total = p.realized + p.remaining * final
        effective = p.entry + total if p.side == "long" else p.entry - total
        return ExitDecision(reason=reason, price=effective, won=total > 0)
