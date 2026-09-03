"""Frozen runtime contracts for active scanner registrations.

Strategy parameters define *where* a scanner fires. These contracts define
the runtime semantics needed to measure that signal honestly: cost family,
maximum holding horizon, and the three clocks that may never be merged:

* structure — one fully closed decision-timeframe candle;
* entry — next-open or BBO acceptance after that close;
* protection — continuous ticks, exits only.

One-minute candles are storage/watchdog bricks and 4h candles are context
vetoes, so neither may be a scanner decision clock. Keeping these rules keyed
by exact strategy ID prevents a timeframe default from silently turning a
swing scanner into a scalp, a forming candle into structure, or a 48-hour
hold into 48 child bars.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

CostFamily = Literal["scalp", "swing"]
DecisionEngine = Literal[
    "base_strategy_next_open_v1",
    "base_strategy_routed_entry_v1",
    "quote_acceptance_v1",
    "quote_acceptance_v2",
]
ExitEngine = Literal["active_exit_v1", "scanner_exit_v1"]
StructureClock = Literal["closed_bar"]
EntryClock = Literal["next_open", "bbo_acceptance", "execution_route"]
ProtectionClock = Literal["ticks"]

_TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3_600,
    "4h": 14_400,
    "1d": 86_400,
}


@dataclass(frozen=True, slots=True)
class ScannerRuntimeContract:
    strategy_id: str
    timeframe: str
    cost_family: CostFamily
    max_holding_bars: int
    rationale: str
    decision_engine: DecisionEngine = "base_strategy_next_open_v1"
    exit_engine: ExitEngine = "active_exit_v1"
    context_timeframes: tuple[str, ...] = ()
    structure_clock: StructureClock = "closed_bar"
    protection_clock: ProtectionClock = "ticks"

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.timeframe:
            raise ValueError("scanner contract identity cannot be empty")
        if self.max_holding_bars < 1:
            raise ValueError("scanner max_holding_bars must be positive")
        if self.timeframe not in _TF_SECONDS:
            raise ValueError(f"unsupported scanner decision timeframe: {self.timeframe}")
        if self.timeframe in {"1m", "4h"}:
            raise ValueError(
                f"{self.timeframe} is not an eligible scanner decision clock; "
                "1m is a brick and 4h is context only"
            )
        if len(set(self.context_timeframes)) != len(self.context_timeframes):
            raise ValueError("scanner context_timeframes cannot contain duplicates")
        decision_seconds = _TF_SECONDS[self.timeframe]
        for context in self.context_timeframes:
            if context not in _TF_SECONDS:
                raise ValueError(f"unsupported scanner context timeframe: {context}")
            if _TF_SECONDS[context] <= decision_seconds:
                raise ValueError(
                    f"scanner context {context} must be slower than decision clock "
                    f"{self.timeframe}"
                )

    @property
    def decision_tf(self) -> str:
        """The only closed-bar clock allowed to create or update a setup."""
        return self.timeframe

    @property
    def context_tfs(self) -> tuple[str, ...]:
        """Last-closed higher-timeframe context; never a fire clock."""
        return self.context_timeframes

    @property
    def entry_clock(self) -> EntryClock:
        if self.decision_engine == "base_strategy_routed_entry_v1":
            return "execution_route"
        return (
            "bbo_acceptance"
            if self.decision_engine.startswith("quote_acceptance")
            else "next_open"
        )

    @property
    def evidence_entry_clock(self) -> str:
        """Unambiguous clock cohort for replay/report aggregation.

        ``entry_clock`` is the legacy runtime enum.  Evidence needs the
        actual clock because a next 5m open and a next 1h open are different
        experiments and must never share one PnL headline.
        """
        if self.decision_engine.startswith("quote_acceptance"):
            return "quote_hold"
        if self.decision_engine == "base_strategy_routed_entry_v1":
            return f"configured_route_after_{self.timeframe}_close"
        return f"next_{self.timeframe}_open"


_CONTRACTS: dict[str, ScannerRuntimeContract] = {
    "squeeze_expansion_breakout_v3": ScannerRuntimeContract(
        strategy_id="squeeze_expansion_breakout_v3",
        timeframe="5m",
        cost_family="scalp",
        max_holding_bars=48,
        rationale="legacy quote-accepted squeeze baseline retained for parity evidence",
        decision_engine="quote_acceptance_v1",
        exit_engine="scanner_exit_v1",
    ),
    "squeeze_expansion_breakout_v4": ScannerRuntimeContract(
        strategy_id="squeeze_expansion_breakout_v4",
        timeframe="5m",
        cost_family="scalp",
        max_holding_bars=48,  # custom acceptance exit: 4-hour backstop
        rationale="quote-accepted short-horizon expansion",
        decision_engine="quote_acceptance_v1",
        exit_engine="scanner_exit_v1",
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
        context_timeframes=("4h",),
    ),
    "structure_bos_15m_trigger_v3": ScannerRuntimeContract(
        strategy_id="structure_bos_15m_trigger_v3",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=192,
        rationale="V2 structure with final-eligibility spacing correction",
        context_timeframes=("4h",),
    ),
    "avwap_reclaim_15m_v1": ScannerRuntimeContract(
        strategy_id="avwap_reclaim_15m_v1",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=48,
        rationale="closed-bar AVWAP reclaim with a 12-hour evidence horizon",
    ),
    "session_continuation_15m_v1": ScannerRuntimeContract(
        strategy_id="session_continuation_15m_v1",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=32,
        rationale="US-overlap continuation with an 8-hour evidence horizon",
    ),
    "liquidity_sweep_reversal_15m_v1": ScannerRuntimeContract(
        strategy_id="liquidity_sweep_reversal_15m_v1",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=32,
        rationale="closed-bar sweep rejection with an 8-hour evidence horizon",
    ),
    "trend_pullback_1h_v1": ScannerRuntimeContract(
        strategy_id="trend_pullback_1h_v1",
        timeframe="1h",
        cost_family="swing",
        max_holding_bars=48,
        rationale="one-hour trend pullback with a 48-hour evidence horizon",
    ),
    "trend_squeeze_continuation_1h_v1": ScannerRuntimeContract(
        strategy_id="trend_squeeze_continuation_1h_v1",
        timeframe="1h",
        cost_family="swing",
        max_holding_bars=12,
        rationale="one-hour squeeze release with a frozen 12-hour horizon",
    ),
    "tick_accepted_breakout_v1": ScannerRuntimeContract(
        strategy_id="tick_accepted_breakout_v1",
        timeframe="5m",
        cost_family="scalp",
        max_holding_bars=48,
        rationale="tick-held acceptance after a closed-bar range arm",
        decision_engine="quote_acceptance_v1",
        exit_engine="scanner_exit_v1",
    ),
    "range_expansion_realtime_v1": ScannerRuntimeContract(
        strategy_id="range_expansion_realtime_v1",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=48,
        rationale="closed-bar expansion context with quote-held breakout entry",
        decision_engine="quote_acceptance_v2",
        exit_engine="scanner_exit_v1",
    ),
    "range_expansion_realtime_v2": ScannerRuntimeContract(
        strategy_id="range_expansion_realtime_v2",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=48,
        rationale=(
            "prior-range boundary pre-armed at the preceding close with quote-held expansion entry"
        ),
        decision_engine="quote_acceptance_v2",
        exit_engine="scanner_exit_v1",
    ),
    "structure_bos_realtime_v1": ScannerRuntimeContract(
        strategy_id="structure_bos_realtime_v1",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=192,
        rationale="confirmed 1h/4h structure with quote-held level break",
        decision_engine="quote_acceptance_v2",
        exit_engine="scanner_exit_v1",
        context_timeframes=("4h",),
    ),
    "structure_bos_realtime_v2": ScannerRuntimeContract(
        strategy_id="structure_bos_realtime_v2",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=192,
        rationale="decision-time aligned confirmed structure with stable swing episode",
        decision_engine="quote_acceptance_v2",
        exit_engine="scanner_exit_v1",
        context_timeframes=("4h",),
    ),
    "session_continuation_realtime_v1": ScannerRuntimeContract(
        strategy_id="session_continuation_realtime_v1",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=32,
        rationale="closed-bar session setup with fill-time session and quote acceptance",
        decision_engine="quote_acceptance_v2",
        exit_engine="scanner_exit_v1",
    ),
    "session_continuation_realtime_v2": ScannerRuntimeContract(
        strategy_id="session_continuation_realtime_v2",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=32,
        rationale="decision-time aligned session boundary with quote-held continuation",
        decision_engine="quote_acceptance_v2",
        exit_engine="scanner_exit_v1",
    ),
    "htf_structure_continuation_realtime_v1": ScannerRuntimeContract(
        strategy_id="htf_structure_continuation_realtime_v1",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=48,
        rationale=("aligned 4h/1h direction with 15m pullback/reclaim and quote-held entry"),
        decision_engine="quote_acceptance_v2",
        exit_engine="scanner_exit_v1",
        context_timeframes=("4h",),
    ),
    "htf_regime_continuation_15m_v1": ScannerRuntimeContract(
        strategy_id="htf_regime_continuation_15m_v1",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=192,
        rationale=(
            "weekly/daily/4h permission with closed 15m reclaim; 48h is a fail-safe "
            "tail while structure deterioration owns the normal exit"
        ),
        decision_engine="base_strategy_next_open_v1",
        exit_engine="scanner_exit_v1",
        context_timeframes=("4h", "1d"),
    ),
    "htf_regime_continuation_15m_v2": ScannerRuntimeContract(
        strategy_id="htf_regime_continuation_15m_v2",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=192,
        rationale=(
            "official-candle weekly range/structure permission without synthetic "
            "VWAP; closed 15m reclaim and next-open entry"
        ),
        decision_engine="base_strategy_next_open_v1",
        exit_engine="scanner_exit_v1",
        context_timeframes=("4h", "1d"),
    ),
    "structure_bounce_route_probe_v2": ScannerRuntimeContract(
        strategy_id="structure_bounce_route_probe_v2",
        timeframe="5m",
        cost_family="scalp",
        max_holding_bars=288,
        rationale=(
            "route-neutral structure bounce cohort; explicit lane policy chooses "
            "Delta taker now or passive retest"
        ),
        decision_engine="base_strategy_routed_entry_v1",
        exit_engine="active_exit_v1",
    ),
}

SCANNER_RUNTIME_CONTRACTS: Mapping[str, ScannerRuntimeContract] = MappingProxyType(_CONTRACTS)


def scanner_runtime_contract(strategy_id: str) -> ScannerRuntimeContract | None:
    return SCANNER_RUNTIME_CONTRACTS.get(strategy_id)


def resolve_scanner_cost_profile(
    contract: ScannerRuntimeContract,
    *,
    exchange_id: str,
) -> str:
    """Combine strategy economics with the venue tariff family.

    Delta profiles include the India GST while preserving the strategy
    family's execution assumptions. Venue selection is explicit here so a
    Delta swing lane cannot silently inherit the untaxed cross-venue fallback.
    """
    if "delta" in exchange_id.lower():
        if contract.cost_family == "scalp":
            return "delta_scalp"
        if contract.cost_family == "swing":
            return "delta_swing"
    return contract.cost_family


__all__ = [
    "SCANNER_RUNTIME_CONTRACTS",
    "DecisionEngine",
    "EntryClock",
    "ExitEngine",
    "ProtectionClock",
    "ScannerRuntimeContract",
    "StructureClock",
    "resolve_scanner_cost_profile",
    "scanner_runtime_contract",
]
