"""Two-sided, quote-held acceptance state for squeeze expansion v3.

Closed bars are used only to establish a causal compression box.  Quotes may
then accept a break during compression or a short grace window.  A failed
probe re-arms that side; it never burns the opposite side.  This object is a
shadow measurement component and has no order-submission dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import Enum

from vnedge.exchange.book_imbalance import BookImbalance, imbalance_allows
from vnedge.execution.trigger_engine import FireDecision, Side
from vnedge.execution.evidence import DecisionEnvelope
from vnedge.strategy.arm_evidence import FrozenPermissionSnapshot
from vnedge.strategy.realtime_entry import StructuralStopMode
from vnedge.strategy.squeeze_expansion_breakout_v3 import (
    PARAMS,
    SqueezeExpansionV3Params,
)


class AcceptanceState(str, Enum):
    DORMANT = "dormant"
    ARMED = "armed"
    PROBE = "probe"
    ACCEPTED = "accepted"
    BURNED = "burned"


@dataclass(frozen=True, slots=True)
class CompressionArm:
    episode_id: int
    box_high: float
    box_low: float
    atr: float
    vwap: float
    bar_index: int
    compressed: bool
    # Generic quote-entry successors may arm one side only and may carry an
    # opposite-structure stop.  Defaults preserve the frozen squeeze contract.
    allow_long: bool = True
    allow_short: bool = True
    long_level: float | None = None
    short_level: float | None = None
    long_structural_stop: float | None = None
    short_structural_stop: float | None = None
    structural_stop_mode: StructuralStopMode = "risk_cap"
    expires_after_bars: int | None = None
    session_start_hour_utc: int | None = None
    session_end_hour_utc: int | None = None
    reason: str = "squeeze_acceptance_v3"
    evidence: FrozenPermissionSnapshot | None = None
    decisions: tuple[DecisionEnvelope, ...] = ()

    def decision_for(self, side: Side) -> DecisionEnvelope | None:
        return next((item for item in self.decisions if item.side == side), None)


@dataclass
class _SideLifecycle:
    state: AcceptanceState = AcceptanceState.DORMANT
    probe_started_at: datetime | None = None
    probe_samples: int = 0
    probes: int = 0
    fires: int = 0
    rearms: int = 0

    def rearm(self) -> None:
        self.rearms += 1
        self.state = AcceptanceState.ARMED
        self.probe_started_at = None
        self.probe_samples = 0


@dataclass
class ExpansionAcceptanceEngine:
    config: SqueezeExpansionV3Params = PARAMS
    require_book_imbalance: bool = False
    min_book_imbalance: float = 0.20
    max_book_spread_ticks: float = 2.0
    arm: CompressionArm | None = None
    arm_expires_bar: int = -1
    position_open: bool = False
    active_side: Side | None = None
    cooldown_until_bar: int = -1
    fires_today: int = 0
    today: date | None = None
    last_reason: str = "no_active_arm"
    long: _SideLifecycle = field(default_factory=_SideLifecycle)
    short: _SideLifecycle = field(default_factory=_SideLifecycle)
    last_quote_ts: datetime | None = None
    last_quote_identity: tuple[object, ...] | None = None
    last_quote_lag_seconds: float = 0.0
    last_quote_source: str = "unknown"
    quotes_seen: int = 0
    quotes_distinct: int = 0
    quote_contract_rejects: int = 0
    quote_overflow_drops: int = 0
    overflow_probe_resets: int = 0
    hold_observation_id: int = 0
    last_hold_ms: float | None = None
    book_filter_rejects: int = 0
    last_book_imbalance: float | None = None
    last_book_spread_ticks: float | None = None

    def _observe_hold(self, started_at: datetime | None, ended_at: datetime) -> None:
        """Expose terminal probe duration as telemetry without changing state."""
        if started_at is None:
            return
        self.last_hold_ms = max(0.0, (ended_at - started_at).total_seconds() * 1000.0)
        self.hold_observation_id += 1

    def note_quote_overflow(self, total_drops: int, *, observed_at: datetime | None = None) -> None:
        """Fail closed when acceptance evidence was evicted upstream.

        A probe cannot prove an uninterrupted hold across missing quotes. Reset
        only in-flight probes; intact arms remain eligible to start again from
        the next distinct, valid observation.
        """
        if total_drops <= self.quote_overflow_drops:
            return
        delta = total_drops - self.quote_overflow_drops
        self.quote_overflow_drops = total_drops
        self.quote_contract_rejects += delta
        for lifecycle in (self.long, self.short):
            if lifecycle.state is AcceptanceState.PROBE:
                if observed_at is not None:
                    self._observe_hold(lifecycle.probe_started_at, observed_at)
                lifecycle.rearm()
                self.overflow_probe_resets += 1
        self.last_reason = "quote_buffer_overflow"

    @property
    def quote_rearms(self) -> int:
        """Number of side lifecycles re-armed since process start."""
        return self.long.rearms + self.short.rearms

    def update_arm(self, arm: CompressionArm) -> None:
        """Refresh from one closed bar without manufacturing a quote fire."""
        if self.position_open:
            self.last_reason = "position_open_arm_deferred"
            return
        valid = (
            all(
                math.isfinite(value) and value > 0
                for value in (arm.box_high, arm.box_low, arm.atr, arm.vwap)
            )
            and arm.box_high > arm.box_low
            and (arm.allow_long or arm.allow_short)
        )
        if not valid:
            self.last_reason = "invalid_arm_features"
            return
        if arm.compressed:
            if self.arm is None or arm.episode_id != self.arm.episode_id:
                long_rearms = self.long.rearms
                short_rearms = self.short.rearms
                self.long = _SideLifecycle(
                    state=(AcceptanceState.ARMED if arm.allow_long else AcceptanceState.DORMANT),
                    rearms=long_rearms,
                )
                self.short = _SideLifecycle(
                    state=(AcceptanceState.ARMED if arm.allow_short else AcceptanceState.DORMANT),
                    rearms=short_rearms,
                )
            self.arm = arm
            grace = arm.expires_after_bars or self.config.arm_grace_bars
            self.arm_expires_bar = arm.bar_index + grace
            if arm.allow_long and arm.allow_short:
                self.last_reason = "armed_both_sides"
            elif arm.allow_long:
                self.last_reason = "armed_long"
            else:
                self.last_reason = "armed_short"
            return
        if (
            self.arm is not None
            and arm.episode_id == self.arm.episode_id
            and arm.bar_index > self.arm_expires_bar
            and not self.position_open
        ):
            # Preserve the frozen compression geometry; only advance the bar
            # clock that controls the grace period.
            self._expire()

    def observe_quote(
        self,
        *,
        bid: float,
        ask: float,
        ts: datetime,
        bar_index: int,
        received_ts: datetime | None = None,
        sequence: int | str | None = None,
        source: str = "unknown",
        exchange_timestamped: bool = False,
        book: BookImbalance | None = None,
    ) -> FireDecision | None:
        self.quotes_seen += 1
        if book is not None:
            self.last_book_imbalance = book.imb
            self.last_book_spread_ticks = book.spread_ticks
        if ts.tzinfo is None:
            raise ValueError("quote timestamp must be timezone-aware")
        received = received_ts or ts
        if received.tzinfo is None:
            raise ValueError("quote receive timestamp must be timezone-aware")
        if not (0 < bid <= ask) or not math.isfinite(bid) or not math.isfinite(ask):
            self.last_reason = "invalid_quote"
            return None
        future_skew = (ts - received).total_seconds()
        if future_skew > self.config.max_quote_future_skew_seconds:
            self.last_reason = "quote_clock_skew"
            self.quote_contract_rejects += 1
            return None
        lag_seconds = max(0.0, (received - ts).total_seconds())
        if exchange_timestamped and lag_seconds > self.config.max_quote_lag_seconds:
            self.last_reason = "quote_ingest_lag"
            self.quote_contract_rejects += 1
            return None
        if self.last_quote_ts is not None and ts < self.last_quote_ts:
            self.last_reason = "quote_out_of_order"
            self.quote_contract_rejects += 1
            return None
        identity: tuple[object, ...]
        if sequence is not None:
            identity = (source, "sequence", sequence)
        else:
            # A venue without a sequence still cannot manufacture acceptance by
            # replaying one unchanged timestamped top-of-book snapshot.
            identity = (source, "quote", ts, bid, ask)
        if identity == self.last_quote_identity:
            self.last_reason = "quote_duplicate"
            self.quote_contract_rejects += 1
            return None
        self.last_quote_identity = identity
        self.last_quote_ts = ts
        self.last_quote_lag_seconds = lag_seconds
        self.last_quote_source = source
        self.quotes_distinct += 1
        self._roll_day(ts)
        if self.arm is None:
            self.last_reason = "no_active_arm"
            return None
        if self.arm.session_start_hour_utc is not None:
            hour = ts.astimezone(UTC).hour
            session_end = self.arm.session_end_hour_utc
            if not (
                session_end is not None and self.arm.session_start_hour_utc <= hour < session_end
            ):
                self.last_reason = "quote_outside_session"
                return None
        if bar_index > self.arm_expires_bar and not self.position_open:
            self._expire()
            return None
        if self.position_open:
            self.last_reason = "one_net_position"
            return None
        if bar_index < self.cooldown_until_bar:
            self.last_reason = "cooldown"
            return None
        if self.fires_today >= self.config.max_fires_per_day:
            self.last_reason = "daily_fire_budget"
            return None

        long_level = self.arm.long_level or (
            self.arm.box_high * (1 + self.config.break_buffer_bps / 10_000)
        )
        short_level = self.arm.short_level or (
            self.arm.box_low * (1 - self.config.break_buffer_bps / 10_000)
        )

        # A quote back inside the box invalidates only that side's probe.
        self._advance_probe(
            "long",
            ask,
            long_level,
            ts,
            crossed=self.arm.allow_long and ask > long_level and ask > self.arm.vwap,
        )
        self._advance_probe(
            "short",
            bid,
            short_level,
            ts,
            crossed=self.arm.allow_short and bid < short_level and bid < self.arm.vwap,
        )

        candidates: tuple[tuple[Side, float, float, _SideLifecycle], ...] = (
            ("long", ask, long_level, self.long),
            ("short", bid, short_level, self.short),
        )
        for side, price, level, lifecycle in candidates:
            if lifecycle.state is not AcceptanceState.PROBE:
                continue
            chase = (
                (price - level) / level if side == "long" else (level - price) / level
            ) * 10_000
            if chase > self.config.max_chase_bps:
                self._observe_hold(lifecycle.probe_started_at, ts)
                lifecycle.state = AcceptanceState.BURNED
                lifecycle.probe_started_at = None
                self.last_reason = f"{side}_chase_burn"
                continue
            held = (ts - (lifecycle.probe_started_at or ts)).total_seconds()
            if (
                lifecycle.probe_samples < self.config.min_acceptance_samples
                or held < self.config.acceptance_hold_seconds
            ):
                self.last_reason = f"{side}_probe"
                continue
            if self.require_book_imbalance:
                book_reject = imbalance_allows(
                    side,
                    book,
                    min_abs=self.min_book_imbalance,
                    max_spread_ticks=self.max_book_spread_ticks,
                )
                if book_reject is not None:
                    self._observe_hold(lifecycle.probe_started_at, ts)
                    lifecycle.rearm()
                    self.book_filter_rejects += 1
                    self.last_reason = book_reject
                    continue
            if lifecycle.fires >= self.config.max_fires_per_side:
                lifecycle.state = AcceptanceState.BURNED
                self.last_reason = f"{side}_fire_budget"
                continue
            distance = self.config.atr_stop_mult * self.arm.atr
            if side == "long":
                atr_stop = price - distance
                structural = self.arm.long_structural_stop
                if structural is None:
                    stop = atr_stop
                elif self.arm.structural_stop_mode == "structure_floor":
                    stop = min(atr_stop, structural)
                else:
                    stop = max(atr_stop, structural)
            else:
                atr_stop = price + distance
                structural = self.arm.short_structural_stop
                if structural is None:
                    stop = atr_stop
                elif self.arm.structural_stop_mode == "structure_floor":
                    stop = max(atr_stop, structural)
                else:
                    stop = min(atr_stop, structural)
            risk = price - stop if side == "long" else stop - price
            if stop <= 0 or risk <= 0:
                lifecycle.state = AcceptanceState.BURNED
                self.last_reason = f"{side}_bad_stop"
                continue
            lifecycle.state = AcceptanceState.ACCEPTED
            self._observe_hold(lifecycle.probe_started_at, ts)
            lifecycle.fires += 1
            self.fires_today += 1
            self.position_open = True
            self.active_side = side
            self.last_reason = f"{side}_accepted"
            return FireDecision(
                side=side,
                level=level,
                box_edge=self.arm.box_high if side == "long" else self.arm.box_low,
                entry=price,
                stop=stop,
                risk=risk,
                episode_id=self.arm.episode_id,
                chase_bps=chase,
                reason=(
                    f"{self.arm.reason} side={side} hold={held:.1f}s "
                    f"samples={lifecycle.probe_samples} chase={chase:.1f}bps "
                    f"episode={self.arm.episode_id} current_quote_entry virtual_only"
                ),
            )
        return None

    def notify_rejected(self) -> None:
        """Central risk rejection is not market evidence; re-arm the side."""
        self._release_active(won=False, rejected=True)

    def notify_flat(self, *, bar_index: int, net_won: bool) -> None:
        side = self.active_side
        self.position_open = False
        self.active_side = None
        self.cooldown_until_bar = bar_index + (
            self.config.cooldown_win_bars if net_won else self.config.cooldown_loss_bars
        )
        if side is None:
            return
        lifecycle = self._side(side)
        if net_won or lifecycle.fires >= self.config.max_fires_per_side:
            lifecycle.state = AcceptanceState.BURNED
        elif self.arm is not None and bar_index <= self.arm_expires_bar:
            lifecycle.rearm()
        else:
            lifecycle.state = AcceptanceState.DORMANT
        self.last_reason = f"{side}_{'won_burned' if net_won else 'loss_rearmed'}"

    def _release_active(self, *, won: bool, rejected: bool = False) -> None:
        side = self.active_side
        self.position_open = False
        self.active_side = None
        if side is not None:
            lifecycle = self._side(side)
            if rejected and lifecycle.fires > 0:
                lifecycle.fires -= 1
                self.fires_today = max(0, self.fires_today - 1)
            lifecycle.rearm()
        self.last_reason = "risk_rejected_rearmed" if rejected else str(won)

    def _advance_probe(
        self, side: Side, price: float, level: float, ts: datetime, *, crossed: bool
    ) -> None:
        lifecycle = self._side(side)
        if lifecycle.state in {
            AcceptanceState.DORMANT,
            AcceptanceState.BURNED,
            AcceptanceState.ACCEPTED,
        }:
            return
        if not crossed:
            if lifecycle.state is AcceptanceState.PROBE:
                self._observe_hold(lifecycle.probe_started_at, ts)
                lifecycle.rearm()
                self.last_reason = f"{side}_probe_failed_rearmed"
            return
        if lifecycle.state is AcceptanceState.ARMED:
            if lifecycle.probes >= self.config.max_probes_per_side:
                lifecycle.state = AcceptanceState.BURNED
                self.last_reason = f"{side}_probe_budget"
                return
            lifecycle.state = AcceptanceState.PROBE
            lifecycle.probes += 1
            lifecycle.probe_started_at = ts
            lifecycle.probe_samples = 1
        else:
            lifecycle.probe_samples += 1

    def _side(self, side: Side) -> _SideLifecycle:
        return self.long if side == "long" else self.short

    def _expire(self) -> None:
        self.long.state = AcceptanceState.DORMANT
        self.short.state = AcceptanceState.DORMANT
        self.arm = None
        self.last_reason = "arm_grace_expired"

    def _roll_day(self, ts: datetime) -> None:
        day = ts.astimezone(UTC).date()
        if day != self.today:
            self.today = day
            self.fires_today = 0
