"""Canonical active-exit decision contract.

``ActiveExitState`` owns mutable trade state. ``ExitEngine`` is the only public
decision façade used by backtest, paper, and live surfaces. Venue-specific fill
models and reduce-only submission remain outside this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from vnedge.strategy.base_strategy import SignalIntent

BREAKEVEN_FEE_BUFFER_BPS = 8.0
TP_PARTIAL_FRACTIONS = (0.40, 0.30)


@dataclass(frozen=True, slots=True)
class ExitEngineConfig:
    """Frozen exit semantics shared by research and runtime surfaces."""

    trail_atr_mult: float = 0.0
    trail_atr_window: int = 14
    max_holding_bars: int | None = 48
    tick_stops_enabled: bool = True
    allow_partial_tp: bool = True
    fee_aware_breakeven_bps: float = BREAKEVEN_FEE_BUFFER_BPS

    def __post_init__(self) -> None:
        if not _non_negative(self.trail_atr_mult):
            raise ValueError("trail_atr_mult must be non-negative")
        if self.trail_atr_window < 1:
            raise ValueError("trail_atr_window must be positive")
        if self.max_holding_bars is not None and self.max_holding_bars < 1:
            raise ValueError("max_holding_bars must be positive or None")
        if not _non_negative(self.fee_aware_breakeven_bps):
            raise ValueError("fee_aware_breakeven_bps must be non-negative")


@dataclass(frozen=True)
class ActiveExitDecision:
    reason: str
    exit_price: float
    quantity: float | None
    final: bool
    active_stop_price: float
    breakeven_armed: bool
    tp_number: int = 0
    tp_reached: int = 0
    mfe_price: float | None = None


@dataclass
class ActiveExitState:
    side: str
    initial_stop_price: float
    take_profit_price: float | None = None
    take_profit_levels: tuple[float, ...] = ()
    entry_price: float | None = None
    original_quantity: float | None = None
    active_stop_price: float | None = None
    breakeven_armed: bool = False
    tp_index: int = 0
    mfe_price: float | None = None
    tp_history: list[float] = field(default_factory=list)
    #: Dynamic ATR-chandelier trail. 0.0 = no trailing (arm-and-lock only, the
    #: legacy behavior). > 0 trails the stop `trail_atr_mult × ATR` behind the
    #: peak favorable price, ratcheted TIGHTER only — a real trailing engine, not
    #: a one-time lock. Shared identically by backtest / shadow / paper / live.
    trail_atr_mult: float = 0.0
    fee_aware_breakeven_bps: float = BREAKEVEN_FEE_BUFFER_BPS

    def __post_init__(self) -> None:
        if self.side not in {"long", "short"}:
            raise ValueError("side must be 'long' or 'short'")
        if not _positive(self.initial_stop_price):
            raise ValueError("initial_stop_price must be positive and finite")
        if self.take_profit_price is not None and not _positive(
            self.take_profit_price
        ):
            raise ValueError("take_profit_price must be positive and finite")
        if not _non_negative(self.trail_atr_mult):
            raise ValueError("trail_atr_mult must be non-negative and finite")
        if not _non_negative(self.fee_aware_breakeven_bps):
            raise ValueError(
                "fee_aware_breakeven_bps must be non-negative and finite"
            )
        self.take_profit_levels = _clean_levels(self.take_profit_levels)

    @classmethod
    def from_signal(
        cls,
        signal: SignalIntent,
        *,
        entry_price: float | None = None,
        quantity: float | None = None,
        trail_atr_mult: float = 0.0,
        fee_aware_breakeven_bps: float = BREAKEVEN_FEE_BUFFER_BPS,
    ) -> ActiveExitState:
        return cls(
            side=signal.side,
            initial_stop_price=float(signal.stop_price),
            take_profit_price=(
                float(signal.take_profit_price)
                if signal.take_profit_price is not None
                else None
            ),
            take_profit_levels=_clean_levels(signal.take_profit_levels),
            entry_price=entry_price if _positive(entry_price) else None,
            original_quantity=quantity if _positive(quantity) else None,
            trail_atr_mult=float(trail_atr_mult),
            fee_aware_breakeven_bps=float(fee_aware_breakeven_bps),
        )

    def seed_entry(self, *, entry_price: float | None, quantity: float | None) -> None:
        if self.entry_price is None and _positive(entry_price):
            assert entry_price is not None
            self.entry_price = float(entry_price)
        if self.original_quantity is None and _positive(quantity):
            assert quantity is not None
            self.original_quantity = abs(float(quantity))

    @property
    def current_stop(self) -> float:
        if self.active_stop_price is not None:
            return self.active_stop_price
        return self.initial_stop_price

    @property
    def ladder(self) -> tuple[float, ...]:
        if not self.take_profit_levels:
            return ()
        if not _positive(self.entry_price):
            return self.take_profit_levels
        assert self.entry_price is not None
        entry = float(self.entry_price)
        if self.side == "long":
            levels = [level for level in self.take_profit_levels if level > entry]
            return tuple(sorted(levels))
        levels = [level for level in self.take_profit_levels if level < entry]
        return tuple(sorted(levels, reverse=True))

    def resolve_bar(
        self,
        *,
        high: float,
        low: float,
        close: float,
        position_quantity: float,
        min_qty: float = 0.0,
        qty_step: float = 0.0,
        max_holding_hit: bool = False,
    ) -> ActiveExitDecision | None:
        """Return the next exit action for this bar, without mutating TP state.

        Stop wins ties, exactly like the backtester. TP state advances only when
        ``mark_accepted`` is called after the reduce-only order is accepted.
        """
        _validate_bar(high=high, low=low, close=close)
        if not math.isfinite(position_quantity):
            raise ValueError("position_quantity must be finite")
        if not _non_negative(min_qty) or not _non_negative(qty_step):
            raise ValueError("min_qty and qty_step must be non-negative and finite")
        if position_quantity <= 0:
            return None
        self._update_mfe(high=high, low=low)
        stop = self.current_stop
        if _stop_crossed(self.side, stop=stop, high=high, low=low):
            reason = "breakeven_stop" if self.breakeven_armed else "stop"
            return ActiveExitDecision(
                reason=reason,
                exit_price=stop,
                quantity=None,
                final=True,
                active_stop_price=stop,
                breakeven_armed=self.breakeven_armed,
                tp_reached=self.tp_reached(),
                mfe_price=self.mfe_price,
            )

        levels = self.ladder
        if levels and self.tp_index < len(levels):
            target = levels[self.tp_index]
            if _target_crossed(self.side, target=target, high=high, low=low):
                tp_number = self.tp_index + 1
                qty, final = self._tp_quantity(
                    tp_number=tp_number,
                    levels_count=len(levels),
                    position_quantity=position_quantity,
                    min_qty=min_qty,
                    qty_step=qty_step,
                )
                return ActiveExitDecision(
                    reason=f"tp{tp_number}_{'final' if final else 'partial'}",
                    exit_price=target,
                    quantity=None if final else qty,
                    final=final,
                    active_stop_price=self.current_stop,
                    breakeven_armed=self.breakeven_armed,
                    tp_number=tp_number,
                    tp_reached=self.tp_reached(),
                    mfe_price=self.mfe_price,
                )

        if not levels and self.take_profit_price is not None:
            target = float(self.take_profit_price)
            if _target_crossed(self.side, target=target, high=high, low=low):
                return ActiveExitDecision(
                    reason="take_profit",
                    exit_price=target,
                    quantity=None,
                    final=True,
                    active_stop_price=self.current_stop,
                    breakeven_armed=self.breakeven_armed,
                    tp_reached=self.tp_reached(),
                    mfe_price=self.mfe_price,
                )

        if max_holding_hit:
            return ActiveExitDecision(
                reason="max_holding",
                exit_price=close,
                quantity=None,
                final=True,
                active_stop_price=self.current_stop,
                breakeven_armed=self.breakeven_armed,
                tp_reached=self.tp_reached(),
                mfe_price=self.mfe_price,
            )
        return None

    def trail_stop(self, atr: float) -> None:
        """Ratchet the stop to an ATR-chandelier level behind the peak favorable
        price. Tighten-ONLY. Call once per bar AFTER a non-exit decide(), so this
        bar's favorable extreme (already in mfe_price) sets a stop that applies to
        LATER bars — never rescued inside its own bar (no intrabar lookahead)."""
        if self.trail_atr_mult <= 0.0 or not _positive(atr) or self.mfe_price is None:
            return
        dist = self.trail_atr_mult * float(atr)
        candidate = (
            self.mfe_price - dist if self.side == "long" else self.mfe_price + dist
        )
        self.active_stop_price = _better_stop(self.side, self.current_stop, candidate)

    def mark_accepted(self, decision: ActiveExitDecision) -> None:
        if decision.tp_number <= 0 or decision.final:
            return
        levels = self.ladder
        # Reconciliation may surface an already-observed fill more than once.
        # Only the next expected acknowledgement may mutate ladder state.
        if decision.tp_number != self.tp_index + 1:
            return
        if decision.tp_number > len(levels):
            return
        expected_price = levels[decision.tp_number - 1]
        if not math.isclose(
            decision.exit_price,
            expected_price,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            return
        self.tp_index = decision.tp_number
        self.tp_history.append(decision.exit_price)
        self._arm_after_tp(decision.tp_number, levels)

    def tp_reached(self) -> int:
        if self.mfe_price is None:
            return 0
        if self.side == "long":
            return sum(1 for level in self.ladder if self.mfe_price >= level)
        return sum(1 for level in self.ladder if self.mfe_price <= level)

    def to_dict(self) -> dict:
        return {
            "entry_price": self.entry_price,
            "original_quantity": self.original_quantity,
            "active_stop_price": self.active_stop_price,
            "breakeven_armed": self.breakeven_armed,
            "tp_index": self.tp_index,
            "mfe_price": self.mfe_price,
            "tp_history": list(self.tp_history),
            "trail_atr_mult": self.trail_atr_mult,
            "fee_aware_breakeven_bps": self.fee_aware_breakeven_bps,
        }

    def restore(self, stored: dict | None) -> None:
        if not stored:
            return
        if _non_negative(stored.get("trail_atr_mult")):
            self.trail_atr_mult = float(stored["trail_atr_mult"])
        if _non_negative(stored.get("fee_aware_breakeven_bps")):
            self.fee_aware_breakeven_bps = float(
                stored["fee_aware_breakeven_bps"]
            )
        if _positive(stored.get("entry_price")):
            self.entry_price = float(stored["entry_price"])
        if _positive(stored.get("original_quantity")):
            self.original_quantity = float(stored["original_quantity"])
        if _positive(stored.get("active_stop_price")):
            # Restored state may tighten protection, never loosen the original
            # signal stop.
            self.active_stop_price = _better_stop(
                self.side,
                self.initial_stop_price,
                float(stored["active_stop_price"]),
            )
        if _positive(stored.get("mfe_price")):
            self.mfe_price = float(stored["mfe_price"])
        self.tp_history = _validated_tp_history(
            stored.get("tp_history", []),
            self.ladder,
        )
        self.tp_index = min(
            len(self.ladder),
            len(self.tp_history),
            max(0, int(stored.get("tp_index", 0) or 0)),
        )
        self.tp_history = self.tp_history[: self.tp_index]
        self.breakeven_armed = self._restored_breakeven_is_protected(stored)

    def _restored_breakeven_is_protected(self, stored: dict) -> bool:
        if (
            not bool(stored.get("breakeven_armed", False))
            or self.tp_index <= 0
            or self.active_stop_price is None
            or not _positive(self.entry_price)
        ):
            return False
        assert self.entry_price is not None
        breakeven = _fee_aware_breakeven(
            self.side,
            self.entry_price,
            self.fee_aware_breakeven_bps,
        )
        if self.side == "long":
            return self.active_stop_price >= breakeven
        return self.active_stop_price <= breakeven

    def _update_mfe(self, *, high: float, low: float) -> None:
        fav = high if self.side == "long" else low
        if self.mfe_price is None:
            self.mfe_price = fav
        elif self.side == "long":
            self.mfe_price = max(self.mfe_price, fav)
        else:
            self.mfe_price = min(self.mfe_price, fav)

    def _tp_quantity(
        self,
        *,
        tp_number: int,
        levels_count: int,
        position_quantity: float,
        min_qty: float,
        qty_step: float,
    ) -> tuple[float, bool]:
        pos_qty = abs(position_quantity)
        if tp_number >= levels_count:
            return pos_qty, True
        fraction = (
            TP_PARTIAL_FRACTIONS[tp_number - 1]
            if tp_number <= len(TP_PARTIAL_FRACTIONS)
            else 0.25
        )
        base_qty = self.original_quantity or pos_qty
        qty = min(pos_qty, base_qty * fraction)
        qty = _round_down(qty, qty_step)
        remaining = pos_qty - qty
        if qty <= 0 or (min_qty > 0 and (qty < min_qty or remaining < min_qty)):
            return pos_qty, True
        return qty, False

    def _arm_after_tp(self, tp_number: int, levels: tuple[float, ...]) -> None:
        if not _positive(self.entry_price):
            return
        assert self.entry_price is not None
        breakeven = _fee_aware_breakeven(
            self.side,
            float(self.entry_price),
            self.fee_aware_breakeven_bps,
        )
        candidate = breakeven
        if tp_number >= 2 and len(levels) >= 1:
            candidate = _better_stop(self.side, candidate, levels[0])
        current = self.current_stop
        self.active_stop_price = _better_stop(self.side, current, candidate)
        self.breakeven_armed = True


@dataclass(slots=True)
class ExitEngine:
    """Thin, side-effect-free façade over one ``ActiveExitState``.

    Same state, config, and market inputs produce the same decision on every
    surface. ``mark_fill`` is the only way a partial TP advances the ladder.
    """

    state: ActiveExitState
    config: ExitEngineConfig = field(default_factory=ExitEngineConfig)

    @classmethod
    def from_signal(
        cls,
        signal: SignalIntent,
        *,
        config: ExitEngineConfig | None = None,
        entry_price: float | None = None,
        quantity: float | None = None,
    ) -> ExitEngine:
        cfg = config or ExitEngineConfig()
        return cls(
            ActiveExitState.from_signal(
                signal,
                entry_price=entry_price,
                quantity=quantity,
                trail_atr_mult=cfg.trail_atr_mult,
                fee_aware_breakeven_bps=cfg.fee_aware_breakeven_bps,
            ),
            cfg,
        )

    def on_bar(
        self,
        *,
        high: float,
        low: float,
        close: float,
        position_quantity: float,
        atr: float = 0.0,
        bars_held: int = 0,
        min_qty: float = 0.0,
        qty_step: float = 0.0,
    ) -> ActiveExitDecision | None:
        max_hit = (
            self.config.max_holding_bars is not None
            and bars_held >= self.config.max_holding_bars
        )
        decision = self.state.resolve_bar(
            high=high,
            low=low,
            close=close,
            position_quantity=position_quantity,
            min_qty=min_qty,
            qty_step=qty_step,
            max_holding_hit=max_hit,
        )
        if decision is None:
            self.state.trail_stop(atr)
            return None
        if not self.config.allow_partial_tp and not decision.final:
            return replace(decision, quantity=None, final=True)
        return decision

    def on_tick(
        self,
        *,
        bid: float,
        ask: float,
        position_quantity: float | None = None,
    ) -> ActiveExitDecision | None:
        """Evaluate stop protection at executable top-of-book prices only."""
        if not self.config.tick_stops_enabled:
            return None
        qty = position_quantity
        if qty is None:
            qty = self.state.original_quantity
        if qty is not None and qty <= 0:
            return None
        stop = self.state.current_stop
        breached = bid <= stop if self.state.side == "long" else ask >= stop
        if not breached:
            return None
        return ActiveExitDecision(
            reason="tick_stop",
            exit_price=stop,
            quantity=None,
            final=True,
            active_stop_price=stop,
            breakeven_armed=self.state.breakeven_armed,
            tp_reached=self.state.tp_reached(),
            mfe_price=self.state.mfe_price,
        )

    def on_strategy_exit(
        self,
        *,
        reason: str,
        price: float,
    ) -> ActiveExitDecision:
        if not reason:
            raise ValueError("strategy exit reason must be non-empty")
        if not _positive(price):
            raise ValueError("strategy exit price must be positive")
        return ActiveExitDecision(
            reason=reason,
            exit_price=float(price),
            quantity=None,
            final=True,
            active_stop_price=self.state.current_stop,
            breakeven_armed=self.state.breakeven_armed,
            tp_reached=self.state.tp_reached(),
            mfe_price=self.state.mfe_price,
        )

    def mark_fill(self, decision: ActiveExitDecision) -> None:
        self.state.mark_accepted(decision)


def _clean_levels(levels: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(float(x) for x in levels if _positive(x))


def _positive(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0


def _non_negative(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _validate_bar(*, high: float, low: float, close: float) -> None:
    if not all(_positive(value) for value in (high, low, close)):
        raise ValueError("bar high, low, and close must be positive and finite")
    if high < low:
        raise ValueError("bar high must be greater than or equal to low")
    if not low <= close <= high:
        raise ValueError("bar close must be inside the high/low range")


def _validated_tp_history(
    stored_history: object,
    levels: tuple[float, ...],
) -> list[float]:
    if not isinstance(stored_history, (list, tuple)):
        return []
    valid: list[float] = []
    for raw, expected in zip(stored_history, levels, strict=False):
        if not _positive(raw) or not math.isclose(
            float(raw),
            expected,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            break
        valid.append(float(raw))
    return valid


def _round_down(quantity: float, step: float) -> float:
    if step <= 0:
        return quantity
    return math.floor((quantity + 1e-12) / step) * step


def _fee_aware_breakeven(side: str, entry_price: float, buffer_bps: float) -> float:
    buffer = buffer_bps / 10_000.0
    return entry_price * (1.0 + buffer) if side == "long" else entry_price * (1.0 - buffer)


def _better_stop(side: str, current: float, candidate: float) -> float:
    return max(current, candidate) if side == "long" else min(current, candidate)


def _stop_crossed(side: str, *, stop: float, high: float, low: float) -> bool:
    return low <= stop if side == "long" else high >= stop


def _target_crossed(side: str, *, target: float, high: float, low: float) -> bool:
    return high >= target if side == "long" else low <= target
