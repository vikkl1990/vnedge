"""Causal closed-candle confirmation for an already-armed setup.

This module is the bar-clock alternative to BBO acceptance.  A slower,
fully-closed structure bar must create the :class:`RealtimeEntryArm` first;
closed 1m candles may then confirm that price held beyond the armed level.

The confirmation close is a *decision* price, never an executable fill.  A
caller must execute at the next 1m open (offline replay) or on a later BBO
(runtime).  Keeping those clocks explicit prevents a closed 1m candle from
quietly becoming a same-close fill.

Research/shadow infrastructure only.  This state machine has no order, sizing,
cost, risk, or capital authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from vnedge.data.candles import Candle
from vnedge.data.symbols import canonical_symbol
from vnedge.strategy.realtime_entry import RealtimeEntryArm

ConfirmationSide = Literal["long", "short"]


@dataclass(frozen=True, slots=True)
class ClosedCandleConfirmationConfig:
    """Frozen contract for a short bar-hold after a slower setup arm."""

    timeframe: str = "1m"
    required_closes: int = 2
    max_wait_bars: int = 3

    def __post_init__(self) -> None:
        if self.timeframe != "1m":
            raise ValueError("closed-candle confirmation currently supports 1m only")
        if self.required_closes < 2:
            raise ValueError("confirmation requires at least two closed candles")
        if self.max_wait_bars < self.required_closes:
            raise ValueError("max_wait_bars cannot be shorter than required_closes")


@dataclass(frozen=True, slots=True)
class ClosedCandleEntryCandidate:
    """A causal decision that still requires a later executable price."""

    symbol: str
    side: ConfirmationSide
    episode_id: int
    armed_at: datetime
    confirmed_at: datetime
    level: float
    confirmation_close: float
    confirmation_bars: int
    decision_clock: Literal["closed_1m"] = "closed_1m"
    execution_clock: Literal["next_1m_open"] = "next_1m_open"


class ClosedCandleConfirmationEngine:
    """Confirm one pre-existing arm using consecutive, distinct closed 1m bars."""

    def __init__(
        self, config: ClosedCandleConfirmationConfig | None = None
    ) -> None:
        self.config = config or ClosedCandleConfirmationConfig()
        self._symbol: str | None = None
        self._arm: RealtimeEntryArm | None = None
        self._armed_at: datetime | None = None
        self._last_open_time: datetime | None = None
        self._side: ConfirmationSide | None = None
        self._count = 0
        self._expired = 0
        self._invalidated = 0

    @property
    def active(self) -> bool:
        return self._arm is not None

    def arm(
        self,
        *,
        symbol: str,
        arm: RealtimeEntryArm,
        armed_at: datetime,
    ) -> None:
        if armed_at.tzinfo is None or armed_at.utcoffset() is None:
            raise ValueError("armed_at must be timezone-aware")
        self._symbol = canonical_symbol(symbol)
        self._arm = arm
        self._armed_at = armed_at
        self._last_open_time = None
        self._side = None
        self._count = 0

    def reset(self) -> None:
        self._symbol = None
        self._arm = None
        self._armed_at = None
        self._last_open_time = None
        self._side = None
        self._count = 0

    def on_closed_candle(
        self, candle: Candle
    ) -> ClosedCandleEntryCandidate | None:
        """Apply one immutable 1m close.

        A forming candle, a different product, or out-of-order identity is a
        contract error.  A close back inside the armed range invalidates the
        consecutive hold but leaves the arm available until its bounded expiry.
        """
        if not candle.is_closed:
            raise ValueError("forming candles cannot confirm an entry")
        if candle.timeframe != self.config.timeframe:
            raise ValueError(
                f"expected {self.config.timeframe} confirmation candle, "
                f"got {candle.timeframe}"
            )
        if self._arm is None or self._symbol is None or self._armed_at is None:
            return None
        if candle.symbol != self._symbol:
            raise ValueError("confirmation candle symbol does not match the armed setup")
        if candle.close_time <= self._armed_at:
            raise ValueError("confirmation candle must close after the setup was armed")
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError("confirmation candles must be strictly ordered and distinct")
        self._last_open_time = candle.open_time

        expiry = self._armed_at + timedelta(
            minutes=self.config.max_wait_bars
        )
        if candle.close_time > expiry:
            self._expired += 1
            self.reset()
            return None

        close = float(candle.close)
        side: ConfirmationSide | None = None
        level = 0.0
        if self._arm.allow_long and close > self._arm.long_level:
            side = "long"
            level = self._arm.long_level
        elif self._arm.allow_short and close < self._arm.short_level:
            side = "short"
            level = self._arm.short_level

        if side is None:
            self._invalidated += 1
            self._side = None
            self._count = 0
            return None

        if side != self._side:
            self._side = side
            self._count = 1
        else:
            self._count += 1
        if self._count < self.config.required_closes:
            return None

        candidate = ClosedCandleEntryCandidate(
            symbol=self._symbol,
            side=side,
            episode_id=self._arm.episode_id,
            armed_at=self._armed_at,
            confirmed_at=candle.close_time,
            level=level,
            confirmation_close=close,
            confirmation_bars=self._count,
        )
        self.reset()
        return candidate

    def stats(self) -> dict[str, int | bool | str | None]:
        return {
            "active": self.active,
            "timeframe": self.config.timeframe,
            "required_closes": self.config.required_closes,
            "max_wait_bars": self.config.max_wait_bars,
            "consecutive_closes": self._count,
            "side": self._side,
            "expired": self._expired,
            "invalidated": self._invalidated,
        }


__all__ = [
    "ClosedCandleConfirmationConfig",
    "ClosedCandleConfirmationEngine",
    "ClosedCandleEntryCandidate",
    "ConfirmationSide",
]
