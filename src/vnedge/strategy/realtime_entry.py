"""Closed-bar setup contract for quote-triggered scanner entries.

The candle plane is allowed to calculate slow, causal context.  It is not
allowed to manufacture an executable entry price.  A ``RealtimeEntryArm``
therefore contains only levels and risk geometry derived from closed bars;
the runtime quote plane decides whether a fresh, executable quote accepts a
level and supplies the entry price.

This contract is research/shadow infrastructure.  It grants no order
authority and does not bypass the normal cost, sizing, portfolio, or risk
gateways.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

StructuralStopMode = Literal["risk_cap", "structure_floor"]


@dataclass(frozen=True, slots=True)
class RealtimeEntryArm:
    """One causal setup waiting for quote-held acceptance."""

    episode_id: int
    bar_index: int
    long_level: float
    short_level: float
    atr: float
    reference_price: float
    allow_long: bool = True
    allow_short: bool = True
    long_structural_stop: float | None = None
    short_structural_stop: float | None = None
    structural_stop_mode: StructuralStopMode = "risk_cap"
    expires_after_bars: int = 1
    session_start_hour_utc: int | None = None
    session_end_hour_utc: int | None = None
    reason: str = "realtime_scanner"

    def __post_init__(self) -> None:
        values = (
            self.long_level,
            self.short_level,
            self.atr,
            self.reference_price,
        )
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("realtime arm price geometry must be finite and positive")
        if self.long_level <= self.short_level:
            raise ValueError("realtime arm long level must exceed short level")
        if not (self.allow_long or self.allow_short):
            raise ValueError("realtime arm must allow at least one side")
        if self.expires_after_bars < 1:
            raise ValueError("realtime arm expiry must be positive")
        if (self.session_start_hour_utc is None) != (self.session_end_hour_utc is None):
            raise ValueError("realtime arm session bounds must be supplied together")
        if self.session_start_hour_utc is not None:
            session_end = self.session_end_hour_utc
            if session_end is None or not (0 <= self.session_start_hour_utc < session_end <= 24):
                raise ValueError("realtime arm UTC session bounds are invalid")
        for stop in (self.long_structural_stop, self.short_structural_stop):
            if stop is not None and (not math.isfinite(stop) or stop <= 0):
                raise ValueError("realtime arm structural stop must be positive")
        if self.structural_stop_mode not in {"risk_cap", "structure_floor"}:
            raise ValueError("realtime arm structural stop mode is invalid")


__all__ = ["RealtimeEntryArm", "StructuralStopMode"]
