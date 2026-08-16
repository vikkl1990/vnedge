"""Measurement-first roster and remaining multi-lane primitives."""

import pandas as pd
import pytest

from vnedge.runtime.multi_lane import LaneSpec, MultiLaneProvider, _build_single_strategy
from vnedge.runtime.multi_lane_shadow import (
    build_capital_lane_specs,
    build_lane_specs_from_env,
    desired_lane_specs,
    lane_specs_fingerprint,
)
from vnedge.runtime.runner_config import RunnerMode
from vnedge.strategy.measurement_only import MeasurementOnly


def test_default_roster_is_measurement_only_and_has_no_capital_lane():
    specs = desired_lane_specs({})
    assert len(specs) == 3
    assert sum(spec.is_primary for spec in specs) == 1
    assert all(spec.strategy_id == "measurement_only_v1" for spec in specs)
    assert all(spec.mode is RunnerMode.SHADOW for spec in specs)
    assert build_capital_lane_specs({}) == []


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


def test_explicit_eligible_strategy_builds_one_paper_lane():
    specs = build_capital_lane_specs(
        {
            "MULTI_LANE_CAPITAL_ENABLED": "1",
            "MULTI_LANE_CAPITAL_STRATEGY": "trend_continuation_v1",
        }
    )
    assert len(specs) == 1
    assert specs[0].mode is RunnerMode.PAPER


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
