"""Trigger plane: armed compression levels -> fire discipline.

RESEARCH_ONLY helper.  It never submits orders: the caller routes a
``FireDecision`` through CostGate, the decision journal, and the shadow /
paper outcome tracker.  This module is the single source of truth for the
fire rules -- ``research/squeeze_trigger_replay.py`` runs the identical
object, so replay evidence and runtime behaviour cannot diverge.

Fire discipline (reviewed spec, 2026-08-18):
- fire only while the scanner reports a compressed box, once per episode;
- bar-close confirmation beyond the buffered level (stand-in for the tick
  plane's 3-10s hold) with a max-chase cap measured from the level -- a
  close too far past the level burns the arm ("move already gone");
- volume confirmation against the box-window average;
- 24h-VWAP side veto (bias filter, never a direction predictor);
- one net position, a per-UTC-day fire budget, minimum spacing between
  fires, and cooldowns after going flat (longer after a loss).

The stop in the decision is anchored to the LEVEL, not the fill, so chase
can never widen risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal

Side = Literal["long", "short"]


@dataclass(frozen=True, slots=True)
class TriggerConfig:
    """Frozen trigger-plane parameters (change requires a new config name)."""

    max_chase_bps: float = 20.0
    entry_slip_bps: float = 1.0
    break_buffer_bps: float = 2.0
    max_fires_per_day: int = 4
    min_bars_between_fires: int = 18
    cooldown_loss_bars: int = 9
    cooldown_win_bars: int = 4
    confirm_close: bool = True
    atr_stop_mult: float = 1.7
    vol_mult: float = 1.3

    def __post_init__(self) -> None:
        if self.max_chase_bps <= 0 or self.entry_slip_bps < 0:
            raise ValueError("chase/slip settings are invalid")
        if self.max_fires_per_day < 1 or self.min_bars_between_fires < 1:
            raise ValueError("budget settings are invalid")
        if self.cooldown_loss_bars < 0 or self.cooldown_win_bars < 0:
            raise ValueError("cooldowns cannot be negative")
        if self.atr_stop_mult <= 0 or self.vol_mult <= 0:
            raise ValueError("stop/volume multipliers must be positive")


@dataclass(frozen=True, slots=True)
class ArmState:
    """One closed-bar snapshot of the scanner's compression state."""

    episode_id: int
    box_high: float
    box_low: float
    compressed: bool
    atr: float
    vol_ma: float
    prev_close: float


@dataclass(frozen=True, slots=True)
class FireDecision:
    side: Side
    level: float
    box_edge: float
    entry: float
    stop: float  # level-anchored, never fill-anchored
    risk: float
    episode_id: int
    chase_bps: float
    reason: str


@dataclass
class TriggerEngine:
    """Stateful fire gate: one net position, per-episode burn, day budget."""

    config: TriggerConfig = field(default_factory=TriggerConfig)
    position_open: bool = False
    fired_episode: int | None = None
    last_fire_bar: int = -(10**9)
    cooldown_until_bar: int = -(10**9)
    fires_today: int = 0
    today: date | None = None

    def _roll_day(self, bar_ts_ms: int) -> None:
        day = datetime.fromtimestamp(bar_ts_ms / 1000, tz=timezone.utc).date()
        if day != self.today:
            self.today = day
            self.fires_today = 0

    def notify_flat(self, bar_index: int, *, won: bool) -> None:
        """Caller reports the position closed; start the cooldown."""
        self.position_open = False
        cooldown = self.config.cooldown_win_bars if won else self.config.cooldown_loss_bars
        self.cooldown_until_bar = bar_index + cooldown

    def try_fire(
        self,
        *,
        arm: ArmState,
        high: float,
        low: float,
        close: float,
        volume: float,
        vwap: float | None,
        bar_index: int,
        bar_ts_ms: int,
    ) -> FireDecision | None:
        self._roll_day(bar_ts_ms)
        c = self.config
        if self.position_open:
            return None
        if not arm.compressed or arm.atr <= 0 or arm.prev_close <= 0:
            return None
        if self.fired_episode == arm.episode_id:
            return None
        if bar_index < self.cooldown_until_bar:
            return None
        if bar_index - self.last_fire_bar < c.min_bars_between_fires:
            return None
        if self.fires_today >= c.max_fires_per_day:
            return None
        if vwap is None or vwap <= 0:
            return None
        if volume <= c.vol_mult * arm.vol_ma:
            return None

        buffer = arm.prev_close * c.break_buffer_bps / 10_000.0
        long_level = arm.box_high + buffer
        short_level = arm.box_low - buffer
        confirmed_long = close > long_level if c.confirm_close else high > long_level
        confirmed_short = close < short_level if c.confirm_close else low < short_level

        side: Side | None = None
        if confirmed_long and arm.prev_close > vwap:
            side, level, box_edge = "long", long_level, arm.box_high
        elif confirmed_short and arm.prev_close < vwap:
            side, level, box_edge = "short", short_level, arm.box_low
        if side is None:
            return None

        chase_bps = (
            (close - level) / level if side == "long" else (level - close) / level
        ) * 10_000.0
        if chase_bps > c.max_chase_bps:
            # Move already gone: burn the arm so this episode never fires.
            self.fired_episode = arm.episode_id
            return None

        if side == "long":
            entry = level * (1.0 + c.entry_slip_bps / 10_000.0)
            stop = level - c.atr_stop_mult * arm.atr
        else:
            entry = level * (1.0 - c.entry_slip_bps / 10_000.0)
            stop = level + c.atr_stop_mult * arm.atr
        if stop <= 0:
            return None

        self.position_open = True
        self.fired_episode = arm.episode_id
        self.last_fire_bar = bar_index
        self.fires_today += 1
        return FireDecision(
            side=side,
            level=level,
            box_edge=box_edge,
            entry=entry,
            stop=stop,
            risk=c.atr_stop_mult * arm.atr,
            episode_id=arm.episode_id,
            chase_bps=chase_bps,
            reason=(
                f"squeeze_fire side={side} chase={chase_bps:.1f}bps "
                f"episode={arm.episode_id} level_anchored_stop virtual_only"
            ),
        )
