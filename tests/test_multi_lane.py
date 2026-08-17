"""Measurement-first roster and remaining multi-lane primitives."""

import pandas as pd
import pytest

from vnedge.runtime.multi_lane import LaneSpec, MultiLaneProvider, _build_single_strategy
from vnedge.runtime.multi_lane_shadow import (
    build_capital_lane_specs,
    build_lane_specs_from_env,
    build_runtime_control,
    build_shadow_observe_lane_specs,
    desired_lane_specs,
    lane_specs_fingerprint,
)
from vnedge.runtime.runner_config import RunnerMode
from vnedge.strategy.measurement_only import MeasurementOnly
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
