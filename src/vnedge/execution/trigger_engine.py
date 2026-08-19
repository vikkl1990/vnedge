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

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Literal

Side = Literal["long", "short"]


class RejectCode(str, Enum):
    """Why a bar with an arm did not produce a fire.

    The coverage analysis attributes missed events to GATED (armed but the
    trigger refused); without these codes that bucket is unauditable, so
    every refusal path sets exactly one.
    """

    IN_POSITION = "in_position"
    NOT_COMPRESSED = "not_compressed"
    BAD_FEATURES = "bad_features"
    EPISODE_BURNED = "episode"
    COOLDOWN = "cooldown"
    SPACING = "spacing"
    BUDGET = "budget"
    NO_VWAP = "no_vwap"
    VOLUME = "volume"
    NO_BREAK = "no_break"
    VWAP_SIDE = "vwap_side"
    CHASE_BURN = "chase_burn"
    BAD_STOP = "bad_stop"


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
    # Percentage band clamping the ATR stop distance. Production paths size
    # stops as a fraction of price (0.55-0.95%) rather than off a 5m ATR,
    # which on BTC is ~3x wider. 0.0 disables either bound.
    stop_pct_floor: float = 0.0
    stop_pct_cap: float = 0.0
    # "atr": distance = atr_stop_mult * ATR (clamped by the pct band).
    # "zone": distance = zone edge + zone_buffer_pct, capped at the tighter of
    # zone_stop_atr_cap * ATR and zone_stop_pct_cap * price -- the production
    # construction, which is much tighter than an ATR stop.
    stop_mode: str = "atr"
    zone_buffer_pct: float = 0.001
    zone_stop_atr_cap: float = 2.0
    zone_stop_pct_cap: float = 0.005
    # "close": pay the market at confirmation.  "retest_limit": rest a limit
    # AT the level and fill only if price comes back to it within
    # retest_expiry_bars.  The second is a maker entry and a better price, paid
    # for with unfilled setups -- never a retroactive fill on the bar that
    # already traded through it.
    entry_mode: str = "close"
    retest_expiry_bars: int = 6

    def __post_init__(self) -> None:
        if self.max_chase_bps <= 0 or self.entry_slip_bps < 0:
            raise ValueError("chase/slip settings are invalid")
        if self.max_fires_per_day < 1 or self.min_bars_between_fires < 1:
            raise ValueError("budget settings are invalid")
        if self.cooldown_loss_bars < 0 or self.cooldown_win_bars < 0:
            raise ValueError("cooldowns cannot be negative")
        if self.atr_stop_mult <= 0:
            raise ValueError("stop multiplier must be positive")
        if self.vol_mult < 0:
            raise ValueError("volume multiplier cannot be negative")
        if self.stop_mode not in ("atr", "zone"):
            raise ValueError("stop_mode must be 'atr' or 'zone'")
        if self.entry_mode not in ("close", "retest_limit"):
            raise ValueError("entry_mode must be 'close' or 'retest_limit'")
        if self.retest_expiry_bars < 1:
            raise ValueError("retest_expiry_bars must be positive")
        if self.stop_pct_floor < 0 or self.stop_pct_cap < 0:
            raise ValueError("stop percentage bounds cannot be negative")
        if self.stop_pct_cap and self.stop_pct_floor > self.stop_pct_cap:
            raise ValueError("stop_pct_floor cannot exceed stop_pct_cap")

    def stop_distance(
        self, *, atr: float, level: float, zone_depth: float | None = None
    ) -> float:
        """Stop distance under the configured construction.

        ``zone_depth`` is how far the stop sits beyond the entry level once the
        zone is cleared; it is only consulted in zone mode.
        """
        if self.stop_mode == "zone":
            distance = (zone_depth or 0.0) + self.zone_buffer_pct * level
            cap = min(self.zone_stop_atr_cap * atr, self.zone_stop_pct_cap * level)
            return min(distance, cap) if cap > 0 else distance
        distance = self.atr_stop_mult * atr
        if self.stop_pct_floor:
            distance = max(distance, self.stop_pct_floor * level)
        if self.stop_pct_cap:
            distance = min(distance, self.stop_pct_cap * level)
        return distance


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
    # Breakout arms leave this None and let the trigger derive the side from
    # which edge broke. Bounce/reversal arms set it: the setup itself decides
    # direction, and `level` becomes the zone edge being defended rather than
    # a level being broken through. Chase is then measured from that edge --
    # "how far past my level am I paying?" -- which is the right question for
    # both entry styles.
    side_hint: Side | None = None


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
    # A pending decision is a resting limit, not a position: the caller fills
    # it only when a LATER bar trades to ``entry``, and drops it at
    # ``expires_bar``.
    pending: bool = False
    expires_bar: int | None = None


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
    last_reject: RejectCode | None = None
    reject_counts: Counter = field(default_factory=Counter)

    def _roll_day(self, bar_ts_ms: int) -> None:
        day = datetime.fromtimestamp(bar_ts_ms / 1000, tz=timezone.utc).date()
        if day != self.today:
            self.today = day
            self.fires_today = 0

    def _reject(self, code: RejectCode) -> None:
        """Record the single reason this bar produced no fire."""
        self.last_reject = code
        self.reject_counts[code.value] += 1

    def notify_cancelled(self, bar_index: int) -> None:
        """Caller reports a resting limit expired unfilled; release the lock."""
        self.position_open = False
        self.last_fire_bar = bar_index

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
        self.last_reject = None
        if self.position_open:
            self._reject(RejectCode.IN_POSITION)
            return None
        if not arm.compressed:
            self._reject(RejectCode.NOT_COMPRESSED)
            return None
        if arm.atr <= 0 or arm.prev_close <= 0:
            self._reject(RejectCode.BAD_FEATURES)
            return None
        if self.fired_episode == arm.episode_id:
            self._reject(RejectCode.EPISODE_BURNED)
            return None
        if bar_index < self.cooldown_until_bar:
            self._reject(RejectCode.COOLDOWN)
            return None
        if bar_index - self.last_fire_bar < c.min_bars_between_fires:
            self._reject(RejectCode.SPACING)
            return None
        if self.fires_today >= c.max_fires_per_day:
            self._reject(RejectCode.BUDGET)
            return None
        if vwap is None or vwap <= 0:
            self._reject(RejectCode.NO_VWAP)
            return None

        # Gate ORDER is the reject taxonomy's meaning: the structural question
        # ("did the box even break?") is asked before the confirmation
        # questions, so a count of `volume` means "a real break with thin
        # volume" rather than "a quiet bar that also had low volume".
        buffer = arm.prev_close * c.break_buffer_bps / 10_000.0
        long_level = arm.box_high + buffer
        short_level = arm.box_low - buffer
        confirmed_long = close > long_level if c.confirm_close else high > long_level
        confirmed_short = close < short_level if c.confirm_close else low < short_level
        if arm.side_hint is None and not (confirmed_long or confirmed_short):
            self._reject(RejectCode.NO_BREAK)
            return None

        if c.vol_mult > 0 and volume <= c.vol_mult * arm.vol_ma:
            self._reject(RejectCode.VOLUME)
            return None

        if arm.side_hint is not None:
            side = arm.side_hint
            if side == "long":
                level, box_edge = arm.box_low, arm.box_low
            else:
                level, box_edge = arm.box_high, arm.box_high
            chase_bps = abs(close - level) / level * 10_000.0
            if chase_bps > c.max_chase_bps:
                self.fired_episode = arm.episode_id
                self._reject(RejectCode.CHASE_BURN)
                return None
            depth = abs(arm.box_high - arm.box_low)
            distance = c.stop_distance(atr=arm.atr, level=level, zone_depth=depth)
            pending = c.entry_mode == "retest_limit"
            entry = level if pending else close
            stop = level - distance if side == "long" else level + distance
            if stop <= 0:
                self._reject(RejectCode.BAD_STOP)
                return None
            self.position_open = True
            self.fired_episode = arm.episode_id
            self.last_fire_bar = bar_index
            self.fires_today += 1
            return FireDecision(
                side=side, level=level, box_edge=box_edge, entry=entry, stop=stop,
                risk=distance, episode_id=arm.episode_id,
                chase_bps=chase_bps, pending=pending,
                expires_bar=bar_index + c.retest_expiry_bars if pending else None,
                reason=(f"bounce_fire side={side} chase={chase_bps:.1f}bps "
                        f"episode={arm.episode_id} zone_anchored_stop "
                        f"entry={c.entry_mode} virtual_only"),
            )

        side: Side | None = None
        if confirmed_long and arm.prev_close > vwap:
            side, level, box_edge = "long", long_level, arm.box_high
        elif confirmed_short and arm.prev_close < vwap:
            side, level, box_edge = "short", short_level, arm.box_low
        if side is None:
            self._reject(RejectCode.VWAP_SIDE)
            return None

        chase_bps = (
            (close - level) / level if side == "long" else (level - close) / level
        ) * 10_000.0
        if chase_bps > c.max_chase_bps:
            # Move already gone: burn the arm so this episode never fires.
            self.fired_episode = arm.episode_id
            self._reject(RejectCode.CHASE_BURN)
            return None

        distance = c.stop_distance(atr=arm.atr, level=level)
        if side == "long":
            entry = level * (1.0 + c.entry_slip_bps / 10_000.0)
            stop = level - distance
        else:
            entry = level * (1.0 - c.entry_slip_bps / 10_000.0)
            stop = level + distance
        if stop <= 0:
            self._reject(RejectCode.BAD_STOP)
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
            risk=distance,
            episode_id=arm.episode_id,
            chase_bps=chase_bps,
            reason=(
                f"squeeze_fire side={side} chase={chase_bps:.1f}bps "
                f"episode={arm.episode_id} level_anchored_stop virtual_only"
            ),
        )
