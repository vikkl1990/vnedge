"""Canonical construction contract for quote-driven scanner engines.

The engine is stateful and clock-agnostic. Live and replay drivers feed the
same two events: a causally prepared closed bar and an executable BBO update.
It has no order authority; an optional approval callback remains the only
bridge to the owning runtime's normal risk boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

import pandas as pd

from vnedge.execution.exit_engine import ExitConfig, ExitEngine
from vnedge.runtime.expansion_acceptance import ExpansionAcceptanceEngine
from vnedge.runtime.scanner_session import SessionCosts
from vnedge.runtime.squeeze_acceptance_observe import SqueezeAcceptanceObserveRunner
from vnedge.runtime.squeeze_observe import FireGuard, JournalSink
from vnedge.strategy.base_strategy import BaseStrategy
from vnedge.strategy.scanner_contracts import ScannerRuntimeContract
from vnedge.strategy.squeeze_expansion_breakout_v3 import SqueezeExpansionV3Params


class ScannerEngine(Protocol):
    """The single event surface shared by runtime and recorded replay."""

    def restore(self, frame: pd.DataFrame) -> None: ...

    def on_closed_bar(
        self, frame: pd.DataFrame, index: int, event_ts: datetime
    ) -> Any: ...

    def on_quote(
        self,
        *,
        bid: float,
        ask: float,
        ts: datetime,
        received_ts: datetime | None = None,
        sequence: int | str | None = None,
        source: str = "unknown",
        exchange_timestamped: bool = False,
        overflow_drops: int = 0,
    ) -> Any: ...


def quote_acceptance_config(strategy: BaseStrategy) -> SqueezeExpansionV3Params:
    """Resolve the exact frozen acceptance contract once for all drivers."""
    configured = getattr(strategy, "acceptance_params", None)
    if isinstance(configured, SqueezeExpansionV3Params):
        return configured
    if strategy.strategy_id == "tick_accepted_breakout_v1":
        return SqueezeExpansionV3Params(
            acceptance_hold_seconds=3.0,
            min_acceptance_samples=3,
            break_buffer_bps=0.0,
        )
    return SqueezeExpansionV3Params()


def build_quote_acceptance_engine(
    *,
    journal: JournalSink,
    symbol: str,
    strategy: BaseStrategy,
    contract: ScannerRuntimeContract,
    cost_profile: str,
    bar_minutes: float,
    approve_fire: FireGuard | None = None,
    notional_usd: float = 3000.0,
    margin_usd: float = 100.0,
) -> SqueezeAcceptanceObserveRunner:
    """Build the canonical quote scanner used by both live and replay.

    Strategy thresholds, exit horizon, fee family, and quote acceptance are
    resolved here. A caller may decide *when* events arrive, but cannot build
    a subtly different engine by duplicating these defaults.
    """
    if not contract.decision_engine.startswith("quote_acceptance"):
        raise ValueError(
            f"{strategy.strategy_id} is not a quote-acceptance scanner "
            f"({contract.decision_engine})"
        )
    if contract.strategy_id != strategy.strategy_id:
        raise ValueError("scanner strategy and runtime contract do not match")
    exit_config = ExitConfig(
        absolute_max_bars=contract.max_holding_bars,
        max_age_bars=contract.max_holding_bars,
        failed_breakout=bool(getattr(strategy, "realtime_failed_breakout", True)),
        breakeven_arm_r=float(getattr(strategy, "realtime_breakeven_arm_r", 1.0)),
        trail_arm_r=float(getattr(strategy, "realtime_trail_arm_r", 2.0)),
        trail_atr_mult=float(getattr(strategy, "realtime_trail_atr_mult", 1.0)),
        tp_ladder=(
            ((float(getattr(strategy, "realtime_reward_r", 2.0)), 1.0),)
            if bool(getattr(strategy, "realtime_fixed_target", False))
            else ()
        ),
    )
    return SqueezeAcceptanceObserveRunner(
        journal=journal,
        symbol=symbol,
        strategy_id=strategy.strategy_id,
        strategy=strategy,
        notional_usd=notional_usd,
        margin_usd=margin_usd,
        acceptance=ExpansionAcceptanceEngine(config=quote_acceptance_config(strategy)),
        exits=ExitEngine(config=exit_config),
        approve_fire=approve_fire,
        costs=SessionCosts.from_profile(cost_profile, bar_minutes=bar_minutes),
    )


__all__ = [
    "ScannerEngine",
    "build_quote_acceptance_engine",
    "quote_acceptance_config",
]
