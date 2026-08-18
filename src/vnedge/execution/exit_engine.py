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

    no_progress_bars: int = 4
    no_progress_min_r: float = 0.5
    breakeven_arm_r: float = 1.0
    trail_arm_r: float = 2.0
    trail_atr_mult: float = 1.0
    absolute_max_bars: int = 48
    taker_bps: float = 5.9
    be_fee_buffer_bps: float = 1.0

    def __post_init__(self) -> None:
        if self.no_progress_bars < 1 or self.no_progress_min_r <= 0:
            raise ValueError("no-progress settings are invalid")
        if self.breakeven_arm_r <= 0 or self.trail_arm_r <= self.breakeven_arm_r - 1e-9:
            raise ValueError("ratchet thresholds are invalid")
        if self.trail_atr_mult <= 0 or self.absolute_max_bars < 1:
            raise ValueError("trail/backstop settings are invalid")
        if self.taker_bps < 0 or self.be_fee_buffer_bps < 0:
            raise ValueError("fee settings are invalid")


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

        # 1) hard SL first (pessimistic within-bar ordering)
        stop_hit = low <= p.stop if side == "long" else high >= p.stop
        if stop_hit:
            price = p.stop
            won = (price > p.entry) if side == "long" else (price < p.entry)
            self.clear()
            return ExitDecision(reason="stop", price=price, won=won)

        # 2) failed breakout: close back inside the box (from the bar after entry)
        if held >= 1:
            back_inside = close < p.box_edge if side == "long" else close > p.box_edge
            if back_inside:
                self.clear()
                return ExitDecision(reason="failed_breakout", price=close, won=False)

        # 3) no progress
        if held >= c.no_progress_bars and p.mfe < c.no_progress_min_r * p.risk:
            self.clear()
            return ExitDecision(reason="no_progress", price=close, won=False)

        # 4) absolute backstop
        if held >= c.absolute_max_bars:
            won = (close > p.entry) if side == "long" else (close < p.entry)
            self.clear()
            return ExitDecision(reason="time_4h", price=close, won=won)

        # 5) ratchets (no exit this bar)
        if p.mfe >= c.breakeven_arm_r * p.risk:
            pad = (c.taker_bps + c.be_fee_buffer_bps) / 10_000.0
            breakeven = p.entry * (1.0 + pad) if side == "long" else p.entry * (1.0 - pad)
            p.stop = max(p.stop, breakeven) if side == "long" else min(p.stop, breakeven)
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
            stop = p.stop
            won = (stop > p.entry) if p.side == "long" else (stop < p.entry)
            self.clear()
            return ExitDecision(reason="stop_tick", price=stop, won=won)
        return None
