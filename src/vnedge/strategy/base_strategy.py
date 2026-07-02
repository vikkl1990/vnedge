"""Strategy interface for the backtester (and later paper/live engines).

Contract that keeps lookahead bias structurally impossible:

- ``prepare()`` may use vectorized operations over the whole frame, but only
  causal ones (rolling windows, shifts backward). It must not leak future
  rows into earlier rows.
- ``signal(df, index)`` is called at the CLOSE of bar ``index`` and may read
  rows ``0..index`` only. The engine fills any resulting intent at the OPEN
  of bar ``index + 1`` — a strategy never trades on information from the bar
  it trades in.
- Every intent must carry a stop price. Stop-less strategies are not
  representable in this system by design.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

import pandas as pd


@dataclass(frozen=True)
class SignalIntent:
    side: Literal["long", "short"]
    stop_price: float
    take_profit_price: float | None = None
    reason: str = ""  # human-readable trigger explanation — explainability is a feature

    def __post_init__(self) -> None:
        if self.stop_price <= 0:
            raise ValueError("stop_price must be positive — stop-less intents are forbidden")


class BaseStrategy(ABC):
    """Subclass and implement prepare() + signal()."""

    #: bars required before signal() is first called (indicator warmup)
    warmup_bars: int = 0
    strategy_id: str = "unnamed"

    @abstractmethod
    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        """Return a copy of ``candles`` with indicator columns added.
        Must not mutate the input frame."""

    @abstractmethod
    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        """Entry decision at the close of bar ``index``. Read rows <= index only."""
