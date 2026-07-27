"""Velocity cohort: a small, clearly-labelled, non-promotable set of fast 5m
shadow lanes for ML-label velocity + a visibly active tape."""

from __future__ import annotations

from vnedge.runtime.multi_lane_shadow import (
    desired_lane_specs,
    velocity_delta_lanes,
)
from vnedge.runtime.runner_config import RunnerMode


def _env(**over):
    # Mirror the deployed compose: the raw 5m sats lanes are OFF (fee-wall
    # losers), so the velocity cohort is the labelled fast-sats path that
    # actually runs. With sats on, the two are semantic twins and dedupe to one.
    base = {
        "MULTI_LANE_EXCHANGES": "binanceusdm,bybit,delta_india",
        "MULTI_LANE_SATS_5M_DELTA": "0",
    }
    base.update(over)
    return base


def test_velocity_lanes_are_labelled_shadow_5m_and_default_on():
    specs = velocity_delta_lanes(_env())
    assert specs, "velocity cohort should be on by default"
    for spec in specs:
        assert spec.lane_id.startswith("velocity_")  # unmistakable in cockpit/journals
        assert spec.timeframe == "5m"
        assert spec.mode is RunnerMode.SHADOW  # never paper, never live
        assert spec.strategy_id == "sats_5m_scalper_v1"
    # kept deliberately small
    assert len(specs) <= 4


def test_velocity_lanes_gate_off():
    assert velocity_delta_lanes(_env(MULTI_LANE_VELOCITY="0")) == []


def test_velocity_lanes_require_delta_exchange():
    assert velocity_delta_lanes(_env(MULTI_LANE_EXCHANGES="binanceusdm,bybit")) == []


def test_velocity_lanes_join_the_desired_roster():
    lane_ids = {s.lane_id for s in desired_lane_specs(_env())}
    assert any(lid.startswith("velocity_") for lid in lane_ids)
    # and every velocity lane in the roster stays SHADOW (unpromotable path)
    for spec in desired_lane_specs(_env()):
        if spec.lane_id.startswith("velocity_"):
            assert spec.mode is RunnerMode.SHADOW


def test_velocity_symbols_are_configurable():
    specs = velocity_delta_lanes(_env(MULTI_LANE_VELOCITY_SYMBOLS="ETH/USDT:USDT"))
    assert len(specs) == 1
