from __future__ import annotations

from vnedge.strategy.scanner_contracts import (
    resolve_scanner_cost_profile,
    scanner_runtime_contract,
)


def test_active_scanner_holds_are_frozen_in_bars_for_their_timeframes() -> None:
    squeeze = scanner_runtime_contract("squeeze_expansion_breakout_v4")
    range_v3 = scanner_runtime_contract("range_expansion_observer_v3")
    bos = scanner_runtime_contract("structure_bos_15m_trigger_v2")

    assert squeeze is not None and (squeeze.timeframe, squeeze.max_holding_bars) == (
        "5m",
        48,
    )
    assert range_v3 is not None and (range_v3.timeframe, range_v3.max_holding_bars) == (
        "15m",
        48,
    )
    assert bos is not None and (bos.timeframe, bos.max_holding_bars) == ("15m", 192)


def test_cost_profile_comes_from_strategy_family_then_venue() -> None:
    squeeze = scanner_runtime_contract("squeeze_expansion_breakout_v4")
    bos = scanner_runtime_contract("structure_bos_15m_trigger_v2")
    assert squeeze is not None and bos is not None

    assert resolve_scanner_cost_profile(squeeze, exchange_id="binanceusdm") == "scalp"
    assert resolve_scanner_cost_profile(squeeze, exchange_id="delta_india") == "delta_scalp"
    assert resolve_scanner_cost_profile(bos, exchange_id="binanceusdm") == "swing"
    assert resolve_scanner_cost_profile(bos, exchange_id="delta_india") == "swing"


def test_unknown_strategy_keeps_legacy_runtime_path() -> None:
    assert scanner_runtime_contract("measurement_only_v1") is None
