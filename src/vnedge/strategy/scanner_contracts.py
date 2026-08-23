"""Frozen runtime contracts for active scanner registrations.

Strategy parameters define *where* a scanner fires.  These contracts define
the runtime semantics needed to measure that signal honestly: cost family and
maximum holding horizon.  Keeping them keyed by exact strategy ID prevents a
timeframe default from silently turning a swing scanner into a scalp, or a
48-hour hold into 48 child bars.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

CostFamily = Literal["scalp", "swing"]


@dataclass(frozen=True, slots=True)
class ScannerRuntimeContract:
    strategy_id: str
    timeframe: str
    cost_family: CostFamily
    max_holding_bars: int
    rationale: str

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.timeframe:
            raise ValueError("scanner contract identity cannot be empty")
        if self.max_holding_bars < 1:
            raise ValueError("scanner max_holding_bars must be positive")


_CONTRACTS: dict[str, ScannerRuntimeContract] = {
    "squeeze_expansion_breakout_v4": ScannerRuntimeContract(
        strategy_id="squeeze_expansion_breakout_v4",
        timeframe="5m",
        cost_family="scalp",
        max_holding_bars=48,  # custom acceptance exit: 4-hour backstop
        rationale="quote-accepted short-horizon expansion",
    ),
    "range_expansion_observer_v3": ScannerRuntimeContract(
        strategy_id="range_expansion_observer_v3",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=48,  # explicitly frozen: 12 hours
        rationale="session expansion with a 12-hour continuation horizon",
    ),
    "range_expansion_observer_v4": ScannerRuntimeContract(
        strategy_id="range_expansion_observer_v4",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=48,
        rationale="V3 setup with final-eligibility spacing correction",
    ),
    "structure_bos_15m_trigger_v2": ScannerRuntimeContract(
        strategy_id="structure_bos_15m_trigger_v2",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=192,  # 48 hours at 15-minute cadence
        rationale="inherits the registered structure_bos_1h 48-hour horizon",
    ),
    "structure_bos_15m_trigger_v3": ScannerRuntimeContract(
        strategy_id="structure_bos_15m_trigger_v3",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=192,
        rationale="V2 structure with final-eligibility spacing correction",
    ),
    "avwap_reclaim_15m_v1": ScannerRuntimeContract(
        strategy_id="avwap_reclaim_15m_v1", timeframe="15m",
        cost_family="swing", max_holding_bars=48,
        rationale="closed-bar AVWAP reclaim with a 12-hour evidence horizon",
    ),
    "session_continuation_15m_v1": ScannerRuntimeContract(
        strategy_id="session_continuation_15m_v1", timeframe="15m",
        cost_family="swing", max_holding_bars=32,
        rationale="US-overlap continuation with an 8-hour evidence horizon",
    ),
    "liquidity_sweep_reversal_15m_v1": ScannerRuntimeContract(
        strategy_id="liquidity_sweep_reversal_15m_v1", timeframe="15m",
        cost_family="swing", max_holding_bars=32,
        rationale="closed-bar sweep rejection with an 8-hour evidence horizon",
    ),
    "trend_pullback_1h_v1": ScannerRuntimeContract(
        strategy_id="trend_pullback_1h_v1", timeframe="1h",
        cost_family="swing", max_holding_bars=48,
        rationale="one-hour trend pullback with a 48-hour evidence horizon",
    ),
    "trend_squeeze_continuation_1h_v1": ScannerRuntimeContract(
        strategy_id="trend_squeeze_continuation_1h_v1", timeframe="1h",
        cost_family="swing", max_holding_bars=12,
        rationale="one-hour squeeze release with a frozen 12-hour horizon",
    ),
    "tick_accepted_breakout_v1": ScannerRuntimeContract(
        strategy_id="tick_accepted_breakout_v1", timeframe="5m",
        cost_family="scalp", max_holding_bars=48,
        rationale="tick-held acceptance after a closed-bar range arm",
    ),
}

SCANNER_RUNTIME_CONTRACTS: Mapping[str, ScannerRuntimeContract] = MappingProxyType(
    _CONTRACTS
)


def scanner_runtime_contract(strategy_id: str) -> ScannerRuntimeContract | None:
    return SCANNER_RUNTIME_CONTRACTS.get(strategy_id)


def resolve_scanner_cost_profile(
    contract: ScannerRuntimeContract,
    *,
    exchange_id: str,
) -> str:
    """Combine strategy economics with the venue tariff family.

    Delta's scalp profile includes the India GST/slippage assumptions.  Swing
    currently has one conservative cross-venue profile; introducing a
    venue-specific swing tariff requires a new CostModel profile and tests,
    never an implicit string convention here.
    """
    if contract.cost_family == "scalp" and "delta" in exchange_id.lower():
        return "delta_scalp"
    return contract.cost_family


__all__ = [
    "SCANNER_RUNTIME_CONTRACTS",
    "ScannerRuntimeContract",
    "resolve_scanner_cost_profile",
    "scanner_runtime_contract",
]
