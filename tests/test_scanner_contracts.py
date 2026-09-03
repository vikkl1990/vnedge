from __future__ import annotations

import pytest

from vnedge.strategy.scanner_contracts import (
    SCANNER_RUNTIME_CONTRACTS,
    ScannerRuntimeContract,
    resolve_scanner_cost_profile,
    scanner_runtime_contract,
)


def test_active_scanner_holds_are_frozen_in_bars_for_their_timeframes() -> None:
    squeeze = scanner_runtime_contract("squeeze_expansion_breakout_v4")
    range_v3 = scanner_runtime_contract("range_expansion_observer_v3")
    range_v4 = scanner_runtime_contract("range_expansion_observer_v4")
    bos = scanner_runtime_contract("structure_bos_15m_trigger_v2")
    bos_v3 = scanner_runtime_contract("structure_bos_15m_trigger_v3")
    trend_squeeze = scanner_runtime_contract("trend_squeeze_continuation_1h_v1")

    assert squeeze is not None and (squeeze.timeframe, squeeze.max_holding_bars) == (
        "5m",
        48,
    )
    assert range_v3 is not None and (range_v3.timeframe, range_v3.max_holding_bars) == (
        "15m",
        48,
    )
    assert range_v4 is not None and (range_v4.timeframe, range_v4.max_holding_bars) == (
        "15m",
        48,
    )
    assert bos is not None and (bos.timeframe, bos.max_holding_bars) == ("15m", 192)
    assert bos_v3 is not None and (bos_v3.timeframe, bos_v3.max_holding_bars) == (
        "15m",
        192,
    )
    assert trend_squeeze is not None and (
        trend_squeeze.timeframe,
        trend_squeeze.max_holding_bars,
    ) == ("1h", 12)


def test_cost_profile_comes_from_strategy_family_then_venue() -> None:
    squeeze = scanner_runtime_contract("squeeze_expansion_breakout_v4")
    bos = scanner_runtime_contract("structure_bos_15m_trigger_v2")
    assert squeeze is not None and bos is not None

    assert resolve_scanner_cost_profile(squeeze, exchange_id="binanceusdm") == "scalp"
    assert resolve_scanner_cost_profile(squeeze, exchange_id="delta_india") == "delta_scalp"
    assert resolve_scanner_cost_profile(bos, exchange_id="binanceusdm") == "swing"
    assert resolve_scanner_cost_profile(bos, exchange_id="delta_india") == "delta_swing"


def test_unknown_strategy_keeps_legacy_runtime_path() -> None:
    assert scanner_runtime_contract("measurement_only_v1") is None


def test_realtime_bos_prearm_prices_the_most_conservative_enabled_swing_venue() -> None:
    from vnedge.plan.cost_model import CostModel
    from vnedge.strategy.realtime_scanners import StructureBosRealtimeV2

    expected = max(
        CostModel.for_profile("swing").round_trip_bps(),
        CostModel.for_profile("delta_swing").round_trip_bps(),
    )
    assert StructureBosRealtimeV2.params.round_trip_cost_bps == expected


def test_active_scanners_publish_their_real_decision_and_exit_engines() -> None:
    squeeze = scanner_runtime_contract("squeeze_expansion_breakout_v4")
    range_expansion = scanner_runtime_contract("range_expansion_observer_v4")
    bos = scanner_runtime_contract("structure_bos_15m_trigger_v3")
    session = scanner_runtime_contract("session_continuation_15m_v1")

    assert squeeze is not None
    assert squeeze.decision_engine == "quote_acceptance_v1"
    assert squeeze.exit_engine == "scanner_exit_v1"
    for contract in (range_expansion, bos, session):
        assert contract is not None
        assert contract.decision_engine == "base_strategy_next_open_v1"
        assert contract.exit_engine == "active_exit_v1"


def test_scanner_clock_contracts_separate_structure_entry_and_protection() -> None:
    squeeze = scanner_runtime_contract("squeeze_expansion_breakout_v4")
    bos = scanner_runtime_contract("structure_bos_15m_trigger_v3")
    assert squeeze is not None and bos is not None

    assert squeeze.decision_tf == "5m"
    assert squeeze.entry_clock == "bbo_acceptance"
    assert squeeze.evidence_entry_clock == "quote_hold"
    assert squeeze.structure_clock == "closed_bar"
    assert squeeze.protection_clock == "ticks"
    assert squeeze.context_tfs == ()

    assert bos.decision_tf == "15m"
    assert bos.entry_clock == "next_open"
    assert bos.evidence_entry_clock == "next_15m_open"
    assert bos.context_tfs == ("4h",)

    routed = scanner_runtime_contract("structure_bounce_route_probe_v2")
    assert routed is not None
    assert routed.decision_tf == "5m"
    assert routed.entry_clock == "execution_route"
    assert routed.evidence_entry_clock == "configured_route_after_5m_close"
    assert routed.cost_family == "scalp"


def test_active_scanner_roster_never_uses_brick_or_context_as_fire_clock() -> None:
    assert SCANNER_RUNTIME_CONTRACTS
    assert {
        contract.decision_tf for contract in SCANNER_RUNTIME_CONTRACTS.values()
    }.isdisjoint({"1m", "4h"})


@pytest.mark.parametrize("timeframe", ["1m", "4h"])
def test_scanner_contract_rejects_non_decision_clocks(timeframe: str) -> None:
    with pytest.raises(ValueError, match="not an eligible scanner decision clock"):
        ScannerRuntimeContract(
            strategy_id="invalid_clock",
            timeframe=timeframe,
            cost_family="swing",
            max_holding_bars=1,
            rationale="test",
        )


def test_scanner_contract_rejects_context_not_slower_than_decision() -> None:
    with pytest.raises(ValueError, match="must be slower"):
        ScannerRuntimeContract(
            strategy_id="invalid_context",
            timeframe="15m",
            context_timeframes=("5m",),
            cost_family="swing",
            max_holding_bars=1,
            rationale="test",
        )
