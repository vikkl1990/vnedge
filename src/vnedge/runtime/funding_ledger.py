"""Funding-print accounting primitives.

Funding is an inventory cash flow, never a scanner clock.  Callers pass an
actual settled print timestamp and rate; this module deliberately knows
nothing about an assumed interval or a next-funding schedule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FundingPrint:
    """One venue-settled funding print.

    ``rate`` is normalized from the long side: a positive value means longs
    pay shorts.  ``ts_ms`` is the venue settlement timestamp.
    """

    ts_ms: int
    rate: float
    source: str = "settled_history"

    def __post_init__(self) -> None:
        if self.ts_ms <= 0:
            raise ValueError("funding print timestamp must be positive")
        if not math.isfinite(self.rate):
            raise ValueError("funding print rate must be finite")

    @property
    def event_id(self) -> str:
        return f"{self.ts_ms}:{self.rate:.16g}"


def funding_cost_usd(*, side: str, notional_usd: float, rate: float) -> float:
    """Return a signed COST (positive=debit, negative=credit).

    Perpetual convention after normalization: positive funding debits a long
    and credits a short.  The caller subtracts this value from book cash/PnL.
    """

    if side not in {"long", "short"}:
        raise ValueError("funding side must be 'long' or 'short'")
    if not math.isfinite(notional_usd) or notional_usd < 0:
        raise ValueError("funding notional must be finite and non-negative")
    if not math.isfinite(rate):
        raise ValueError("funding rate must be finite")
    direction = 1.0 if side == "long" else -1.0
    return direction * notional_usd * rate
