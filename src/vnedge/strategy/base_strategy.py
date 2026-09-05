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
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

import pandas as pd

from vnedge.data.candles import floor_time
from vnedge.execution.evidence import DecisionEnvelope
from vnedge.strategy.arm_evidence import FrozenPermissionSnapshot, freeze_permission_from_row
from vnedge.strategy.realtime_entry import RealtimeEntryArm


@dataclass(frozen=True)
class SignalIntent:
    side: Literal["long", "short"]
    stop_price: float
    take_profit_price: float | None = None
    #: Optional TP ladder (tp1, tp2, tp3 …). Strategies that emit a ladder use
    #: the runtime active-exit policy: partial at TP1/TP2, fee-aware breakeven
    #: after TP1, and final close on the last level. Strategies without a
    #: ladder keep the classic single ``take_profit_price`` full-close behavior.
    take_profit_levels: tuple[float, ...] = ()
    reason: str = ""  # human-readable trigger explanation — explainability is a feature
    #: Optional passive-entry level.  It is ignored by taker routes.  A
    #: maker-retest route must use this exact level for CostGate, sizing,
    #: journaling, shadow touch-to-fill, and eventual venue submission. Kept
    #: after ``reason`` so historical four-positional-argument calls retain
    #: their meaning.
    entry_limit_price: float | None = None
    #: Immutable decision/context permission carried from setup through the
    #: execution kernel. Context-aware strategies fail closed without it.
    permission_snapshot: FrozenPermissionSnapshot | None = None
    #: ARM identity minted from this strategy's closed decision bar. Runtime
    #: entry paths refuse new risk when this is absent.
    decision_envelope: DecisionEnvelope | None = None

    def __post_init__(self) -> None:
        if self.stop_price <= 0:
            raise ValueError("stop_price must be positive — stop-less intents are forbidden")
        if self.entry_limit_price is not None and self.entry_limit_price <= 0:
            raise ValueError("entry_limit_price must be positive when supplied")


def bind_signal_decision(
    signal: SignalIntent,
    *,
    strategy_id: str,
    symbol: str,
    timeframe: str,
    decision_row: Mapping[str, object],
    entry_clock: str,
    require_existing_snapshot: bool = False,
) -> SignalIntent:
    """Mint a next-open ARM envelope from one closed decision row.

    Context-aware strategies must supply the snapshot built from their bound
    HTF frames.  Context-free strategies receive a decision-bar-only snapshot;
    the runtime never manufactures HTF references from calendar flooring.
    """

    if signal.decision_envelope is not None:
        return signal
    snapshot = signal.permission_snapshot
    if snapshot is None:
        if require_existing_snapshot:
            raise ValueError("required permission snapshot missing at ARM")
        try:
            snapshot = freeze_permission_from_row(
                decision_row,
                decision_timeframe=timeframe,
                context_timeframes=(),
                allow_long=signal.side == "long",
                allow_short=signal.side == "short",
                reason=signal.reason or strategy_id,
            )
        except ValueError as exc:
            # Old unit fixtures predate TF-aligned candle identity. Keep this
            # compatibility strictly outside canonical/router data: the raw
            # timestamp remains inside the content hash, while production
            # malformed bars still fail closed.
            source = str(decision_row.get("candle_source", "unreported"))
            if "not timeframe-aligned" not in str(exc) or source != "unreported":
                raise
            raw_open = pd.Timestamp(
                decision_row.get("timestamp", decision_row.get("open_time"))
            )
            if raw_open.tzinfo is None:
                raw_open = raw_open.tz_localize("UTC")
            else:
                raw_open = raw_open.tz_convert("UTC")
            normalized_row = dict(decision_row)
            normalized_row["source_open_time"] = raw_open.isoformat()
            normalized_row["timestamp"] = floor_time(
                raw_open.to_pydatetime(), timeframe
            )
            snapshot = freeze_permission_from_row(
                normalized_row,
                decision_timeframe=timeframe,
                context_timeframes=(),
                allow_long=signal.side == "long",
                allow_short=signal.side == "short",
                reason=signal.reason or strategy_id,
            )
    decision = DecisionEnvelope.create(
        strategy_id=strategy_id,
        symbol=symbol,
        timeframe=timeframe,
        side=signal.side,
        permission_snapshot=snapshot,
        entry_clock=entry_clock,
    )
    return replace(
        signal,
        permission_snapshot=snapshot,
        decision_envelope=decision,
    )


@dataclass(frozen=True)
class StrategyExitIntent:
    """Strategy-managed close request for an already-open position.

    This is intentionally narrower than ``SignalIntent``: it can only close an
    existing position, never open or resize one. Runners still submit the actual
    reduce-only order through their normal gateway/journal/order-manager path.
    """

    reason: str
    exit_price: float | None = None

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("exit reason must be non-empty")
        if self.exit_price is not None and self.exit_price <= 0:
            raise ValueError("exit_price must be positive when supplied")


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

    def realtime_arm(
        self, df: pd.DataFrame, index: int
    ) -> RealtimeEntryArm | None:
        """Optional closed-bar setup for a quote-triggered entry.

        Returning an arm moves entry semantics to the runtime quote plane.
        Such a strategy must keep ``signal()`` silent so one market setup can
        never enter through both the bar and quote paths.  The untyped return
        avoids a strategy/runtime import cycle; concrete implementations
        return :class:`vnedge.strategy.realtime_entry.RealtimeEntryArm`.
        """
        del df, index
        return None

    def exit_signal(
        self,
        df: pd.DataFrame,
        index: int,
        side: str,
        entry_price: float,
    ) -> StrategyExitIntent | None:
        """Optional strategy-managed exit at the close of bar ``index``.

        Implementations may read only rows <= ``index``. Stop/TP capital
        protection remains runner-owned and is evaluated before this hook.
        Returning an intent asks the runner to close reduce-only at bar close.
        """
        return None

    def synthesize_exit_plan(
        self, df: pd.DataFrame, index: int, side: str, entry_price: float
    ) -> SignalIntent | None:
        """Rebuild a protective exit plan for a restored position whose
        original plan was lost (legacy account snapshots predating plan
        persistence). Return None (default) if the strategy cannot do this
        honestly — the runner then falls back to the orphan guard (entries
        halted, manual flatten). Implementations must use the SAME formulas
        as signal() so the rebuilt stop/target match an uninterrupted run as
        closely as the data allows."""
        return None
