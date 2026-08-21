"""Measurement-first roster and remaining multi-lane primitives."""

import json

import pandas as pd
import pytest

from vnedge.runtime.multi_lane import LaneSpec, MultiLaneProvider, _build_single_strategy
from vnedge.runtime.multi_lane_shadow import (
    build_capital_lane_specs,
    build_lane_specs_from_env,
    build_runtime_control,
    build_shadow_observe_lane_specs,
    build_shadow_observe_roster_specs,
    desired_lane_specs,
    lane_specs_fingerprint,
)
from vnedge.runtime.runner_config import RunnerMode
from vnedge.strategy.fee_wall_momentum_observer import FeeWallMomentumObserver
from vnedge.strategy.measurement_only import MeasurementOnly
from vnedge.strategy.range_expansion_observer import RangeExpansionObserver
from vnedge.strategy.squeeze_expansion_breakout import SqueezeExpansionBreakout
from vnedge.strategy.squeeze_expansion_breakout_v3 import SqueezeExpansionBreakoutV3
from vnedge.strategy.structure_bos_1h import StructureBos1H


def test_default_roster_is_measurement_only_and_has_no_capital_lane():
    specs = desired_lane_specs({})
    assert len(specs) == 3
    assert sum(spec.is_primary for spec in specs) == 1
    assert all(spec.strategy_id == "measurement_only_v1" for spec in specs)
    assert all(spec.mode is RunnerMode.SHADOW for spec in specs)
    assert build_capital_lane_specs({}) == []
    assert build_shadow_observe_lane_specs({}) == []


def test_measurement_grid_expands_and_normalizes_delta_symbol():
    specs = build_lane_specs_from_env(
        {
            "MULTI_LANE_EXCHANGES": "binanceusdm,delta_india",
            "MULTI_LANE_SYMBOLS": "BTC/USDT:USDT,ETH/USDT:USDT",
            "MULTI_LANE_TIMEFRAME": "15m",
        }
    )
    assert len(specs) == 4
    assert {s.timeframe for s in specs} == {"15m"}
    assert {s.symbol for s in specs if s.exchange == "delta_india"} == {
        "BTC/USD:USD",
        "ETH/USD:USD",
    }


def test_measurement_strategy_never_emits_signal():
    strategy = _build_single_strategy("measurement_only_v1", {}, None, None)
    assert isinstance(strategy, MeasurementOnly)
    frame = pd.DataFrame({"close": [100.0, 101.0]})
    assert strategy.signal(strategy.prepare(frame), 1) is None


def test_shadow_observe_roster_is_explicit_virtual_only():
    env = {
        "MULTI_LANE_EXCHANGES": "binanceusdm",
        "MULTI_LANE_SHADOW_OBSERVE_ENABLED": "1",
        "MULTI_LANE_SHADOW_OBSERVE_STRATEGY": "structure_bos_1h",
        "MULTI_LANE_SHADOW_OBSERVE_EXCHANGE": "binanceusdm",
        "MULTI_LANE_SHADOW_OBSERVE_SYMBOL": "BTC/USDT:USDT",
    }
    specs = desired_lane_specs(env)
    observe = [spec for spec in specs if spec.lane_id.startswith("shadow_observe_")]
    assert len(observe) == 1
    assert observe[0].strategy_id == "structure_bos_1h"
    assert observe[0].mode is RunnerMode.SHADOW
    assert observe[0].timeframe == "1h"
    assert not observe[0].is_primary
    control = build_runtime_control(specs)
    assert control["shadow_observe_enabled"] is True
    assert control["shadow_observe_strategy"] == "structure_bos_1h"
    assert control["shadow_observe_lanes"] == 1
    assert control["paper_lanes"] == 0
    assert control["orders_allowed"] is False
    assert control["live_orders_allowed"] is False


def test_fee_wall_shadow_observe_expands_btc_eth_virtual_lanes():
    env = {
        "MULTI_LANE_EXCHANGES": "binanceusdm",
        "MULTI_LANE_SHADOW_OBSERVE_ENABLED": "1",
        "MULTI_LANE_SHADOW_OBSERVE_STRATEGY": "fee_wall_momentum_observer_v1",
        "MULTI_LANE_SHADOW_OBSERVE_EXCHANGE": "binanceusdm",
        "MULTI_LANE_SHADOW_OBSERVE_SYMBOLS": "BTC/USDT:USDT,ETH/USDT:USDT",
        "MULTI_LANE_SHADOW_OBSERVE_TIMEFRAME": "5m",
    }
    observe = build_shadow_observe_lane_specs(env)

    assert len(observe) == 2
    assert {spec.symbol for spec in observe} == {
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
    }
    assert all(spec.strategy_id == "fee_wall_momentum_observer_v1" for spec in observe)
    assert all(spec.mode is RunnerMode.SHADOW for spec in observe)
    assert all(not spec.is_primary for spec in observe)


@pytest.mark.parametrize(
    "env, message",
    [
        ({"MULTI_LANE_SHADOW_OBSERVE_ENABLED": "1"}, "requires both"),
        (
            {"MULTI_LANE_SHADOW_OBSERVE_STRATEGY": "structure_bos_1h"},
            "requires both",
        ),
        (
            {
                "MULTI_LANE_SHADOW_OBSERVE_ENABLED": "1",
                "MULTI_LANE_SHADOW_OBSERVE_STRATEGY": "funding_mean_reversion_v1",
            },
            "not shadow-observe eligible",
        ),
        (
            {
                "MULTI_LANE_SHADOW_OBSERVE_ENABLED": "1",
                "MULTI_LANE_SHADOW_OBSERVE_STRATEGY": "structure_bos_1h",
                "MULTI_LANE_SHADOW_OBSERVE_TIMEFRAME": "5m",
            },
            "requires timeframe 1h",
        ),
        (
            {
                "MULTI_LANE_SHADOW_OBSERVE_ENABLED": "1",
                "MULTI_LANE_SHADOW_OBSERVE_STRATEGY": "fee_wall_momentum_observer_v1",
                "MULTI_LANE_SHADOW_OBSERVE_TIMEFRAME": "1h",
            },
            "requires timeframe 5m",
        ),
    ],
)
def test_shadow_observe_roster_fails_closed(env, message):
    with pytest.raises(ValueError, match=message):
        build_shadow_observe_lane_specs(env)


def test_structure_bos_lane_factory_uses_frozen_cost_gated_strategy():
    strategy = _build_single_strategy("structure_bos_1h", {}, None, None)
    assert isinstance(strategy, StructureBos1H)
    with pytest.raises(ValueError, match="parameters are frozen"):
        _build_single_strategy("structure_bos_1h", {"left": 2}, None, None)


def test_fee_wall_lane_factory_uses_frozen_virtual_observer():
    strategy = _build_single_strategy(
        "fee_wall_momentum_observer_v1", {}, None, None
    )
    assert isinstance(strategy, FeeWallMomentumObserver)
    with pytest.raises(ValueError, match="parameters are frozen"):
        _build_single_strategy(
            "fee_wall_momentum_observer_v1", {"fee_wall_bps": 14}, None, None
        )


def test_squeeze_lane_factory_uses_canonical_scanner_strategy():
    strategy = _build_single_strategy(
        "squeeze_expansion_breakout_v2", {}, None, None
    )
    assert isinstance(strategy, SqueezeExpansionBreakout)
    with pytest.raises(ValueError, match="parameters are frozen"):
        _build_single_strategy(
            "squeeze_expansion_breakout_v2", {"compression_bars": 24}, None, None
        )


def test_squeeze_v3_lane_factory_uses_quote_acceptance_strategy():
    strategy = _build_single_strategy(
        "squeeze_expansion_breakout_v3", {}, None, None
    )
    assert isinstance(strategy, SqueezeExpansionBreakoutV3)
    with pytest.raises(ValueError, match="parameters are frozen"):
        _build_single_strategy(
            "squeeze_expansion_breakout_v3", {"arm_grace_bars": 4}, None, None
        )


def test_squeeze_v3_shadow_roster_requires_5m() -> None:
    env = {
        "MULTI_LANE_SHADOW_OBSERVE_ENABLED": "1",
        "MULTI_LANE_SHADOW_OBSERVE_STRATEGY": "squeeze_expansion_breakout_v3",
        "MULTI_LANE_SHADOW_OBSERVE_TIMEFRAME": "5m",
    }
    specs = build_shadow_observe_lane_specs(env)
    assert len(specs) == 1
    assert specs[0].strategy_id == "squeeze_expansion_breakout_v3"
    assert specs[0].timeframe == "5m"


def test_range_expansion_lane_factory_and_roster_are_frozen() -> None:
    strategy = _build_single_strategy(
        "range_expansion_observer_v1", {}, None, None
    )
    assert isinstance(strategy, RangeExpansionObserver)
    env = {
        "MULTI_LANE_SHADOW_OBSERVE_ENABLED": "1",
        "MULTI_LANE_SHADOW_OBSERVE_STRATEGY": "range_expansion_observer_v1",
        "MULTI_LANE_SHADOW_OBSERVE_TIMEFRAME": "1h",
    }
    assert build_shadow_observe_lane_specs(env)[0].strategy_id == strategy.strategy_id
    with pytest.raises(ValueError, match="parameters are frozen"):
        _build_single_strategy(
            "range_expansion_observer_v1", {"range_bars": 6}, None, None
        )


def _write_observer_roster(tmp_path, observers: list[dict]):
    path = tmp_path / "observers.json"
    path.write_text(
        json.dumps({"version": 1, "observers": observers}), encoding="utf-8"
    )
    return path


def test_versioned_roster_runs_5m_and_1h_observers_concurrently(tmp_path) -> None:
    path = _write_observer_roster(
        tmp_path,
        [
            {
                "strategy_id": "squeeze_expansion_breakout_v3",
                "exchange": "binanceusdm",
                "symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
                "timeframe": "5m",
                "starting_equity": 1000,
                "daily_loss_usd": 20,
            },
            {
                "strategy_id": "range_expansion_observer_v1",
                "exchange": "binanceusdm",
                "symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
                "timeframe": "1h",
            },
            {
                "strategy_id": "structure_bos_1h",
                "exchange": "binanceusdm",
                "symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
                "timeframe": "1h",
            },
        ],
    )
    env = {"MULTI_LANE_SHADOW_OBSERVE_ROSTER_PATH": str(path)}
    specs = build_shadow_observe_roster_specs(env)

    assert len(specs) == 6
    assert len({spec.lane_id for spec in specs}) == 6
    assert {spec.timeframe for spec in specs} == {"5m", "1h"}
    assert {spec.strategy_id for spec in specs} == {
        "squeeze_expansion_breakout_v3",
        "range_expansion_observer_v1",
        "structure_bos_1h",
    }
    assert all(spec.mode is RunnerMode.SHADOW for spec in specs)
    control = build_runtime_control(specs)
    assert control["shadow_observe_strategy"] == "multiple"
    assert control["shadow_observe_timeframes"] == ["1h", "5m"]


def test_checked_in_observer_roster_is_valid() -> None:
    specs = build_shadow_observe_roster_specs(
        {"MULTI_LANE_SHADOW_OBSERVE_ROSTER_PATH": "config/shadow-observers.v1.json"}
    )
    assert len(specs) == 6
    assert all(not spec.is_primary for spec in specs)


def test_observer_roster_cannot_mix_with_legacy_contract(tmp_path) -> None:
    path = _write_observer_roster(
        tmp_path,
        [{
            "strategy_id": "structure_bos_1h",
            "exchange": "binanceusdm",
            "symbols": ["BTC/USDT:USDT"],
            "timeframe": "1h",
        }],
    )
    with pytest.raises(ValueError, match="cannot be mixed"):
        build_shadow_observe_roster_specs(
            {
                "MULTI_LANE_SHADOW_OBSERVE_ROSTER_PATH": str(path),
                "MULTI_LANE_SHADOW_OBSERVE_ENABLED": "1",
                "MULTI_LANE_SHADOW_OBSERVE_STRATEGY": "structure_bos_1h",
            }
        )


@pytest.mark.parametrize(
    "observer, message",
    [
        (
            {
                "strategy_id": "squeeze_expansion_breakout_v3",
                "exchange": "binanceusdm",
                "symbols": ["BTC/USDT:USDT"],
                "timeframe": "1h",
            },
            "requires timeframe 5m",
        ),
        (
            {
                "strategy_id": "structure_bos_1h",
                "exchange": "binanceusdm",
                "symbols": ["BTC/USDT:USDT"],
                "timeframe": "1h",
                "live": True,
            },
            "unknown fields",
        ),
    ],
)
def test_observer_roster_fails_closed_on_bad_contract(
    tmp_path, observer, message
) -> None:
    path = _write_observer_roster(tmp_path, [observer])
    with pytest.raises(ValueError, match=message):
        build_shadow_observe_roster_specs(
            {"MULTI_LANE_SHADOW_OBSERVE_ROSTER_PATH": str(path)}
        )


def test_observer_roster_rejects_duplicate_stable_lane_ids(tmp_path) -> None:
    path = _write_observer_roster(
        tmp_path,
        [{
            "strategy_id": "structure_bos_1h",
            "exchange": "binanceusdm",
            "symbols": ["BTC/USDT:USDT", "BTC/USDT:USDT"],
            "timeframe": "1h",
        }],
    )
    with pytest.raises(ValueError, match="duplicate lane ids"):
        build_shadow_observe_roster_specs(
            {"MULTI_LANE_SHADOW_OBSERVE_ROSTER_PATH": str(path)}
        )


@pytest.mark.parametrize(
    "env",
    [
        {"MULTI_LANE_CAPITAL_ENABLED": "1"},
        {"MULTI_LANE_CAPITAL_STRATEGY": "trend_continuation_v1"},
    ],
)
def test_capital_roster_requires_both_explicit_gates(env):
    with pytest.raises(ValueError, match="requires both"):
        build_capital_lane_specs(env)


def test_unknown_and_killed_strategies_cannot_enter_capital_roster():
    with pytest.raises(KeyError):
        build_capital_lane_specs(
            {"MULTI_LANE_CAPITAL_ENABLED": "1", "MULTI_LANE_CAPITAL_STRATEGY": "gone"}
        )
    with pytest.raises(ValueError, match="not capital eligible"):
        build_capital_lane_specs(
            {
                "MULTI_LANE_CAPITAL_ENABLED": "1",
                "MULTI_LANE_CAPITAL_STRATEGY": "funding_mean_reversion_v1",
            }
        )


def test_registered_but_unapproved_strategy_cannot_build_a_paper_lane():
    with pytest.raises(ValueError, match="not capital eligible"):
        build_capital_lane_specs(
            {
                "MULTI_LANE_CAPITAL_ENABLED": "1",
                "MULTI_LANE_CAPITAL_STRATEGY": "trend_continuation_v1",
            }
        )


def test_lane_capital_downgrade_is_fail_closed():
    spec = LaneSpec(
        "bad",
        "binanceusdm",
        "BTC/USDT:USDT",
        strategy_id="funding_mean_reversion_v1",
        mode=RunnerMode.PAPER,
    )
    assert spec.capital_downgraded().mode is RunnerMode.SHADOW


def test_provider_and_fingerprint_are_stable():
    specs = build_lane_specs_from_env({"MULTI_LANE_EXCHANGES": "binanceusdm"})
    provider = MultiLaneProvider(specs[0].lane_id)
    provider.publish_warming(specs[0].lane_id, specs[0].exchange, specs[0].symbol)
    assert provider.latest()["lanes"][0]["strategy_id"] == ""
    assert lane_specs_fingerprint(specs) == lane_specs_fingerprint(specs)
    assert lane_specs_fingerprint(specs) == lane_specs_fingerprint(list(reversed(specs)))
    changed = [LaneSpec("other", "binanceusdm", "ETH/USDT:USDT")]
    assert lane_specs_fingerprint(specs) != lane_specs_fingerprint(changed)


def test_provider_labels_shadow_observe_without_granting_order_permission():
    specs = build_shadow_observe_lane_specs(
        {
            "MULTI_LANE_SHADOW_OBSERVE_ENABLED": "1",
            "MULTI_LANE_SHADOW_OBSERVE_STRATEGY": "structure_bos_1h",
        }
    )
    provider = MultiLaneProvider(specs[0].lane_id, lane_specs=specs)
    provider.publish_warming(specs[0].lane_id, specs[0].exchange, specs[0].symbol)
    lane = provider.latest()["lanes"][0]
    assert lane["observation_class"] == "shadow_observe"
    assert lane["mode"] == "warming up"
