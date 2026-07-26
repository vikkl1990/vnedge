"""Evidence-aligned Delta shadow roster (second_eye_grid backtest).

Guards the roster realignment: the grid-proven 4h/1h survivors are enrolled as
Delta-India SHADOW lanes, ``desired_lane_specs`` includes them, and the lane
factory can construct ``vnedge_algo_ml_pro`` — previously unwired in the runtime,
so a lane for it would have raised ``unsupported lane strategy_id``.
"""

from vnedge.runtime.multi_lane import _build_single_strategy
from vnedge.runtime.multi_lane_shadow import (
    EVIDENCE_ALIGNED_DELTA_LANES,
    desired_lane_specs,
    evidence_aligned_shadow_lanes,
)
from vnedge.runtime.runner_config import RunnerMode
from vnedge.strategy.vnedge_algo_ml_pro import VNEDGEAlgoMLProScanner


def test_evidence_aligned_lanes_are_shadow_delta_and_slow_tf():
    lanes = evidence_aligned_shadow_lanes({})

    assert len(lanes) == len(EVIDENCE_ALIGNED_DELTA_LANES)
    assert all(lane.exchange == "delta_india" for lane in lanes)
    assert all(lane.mode is RunnerMode.SHADOW for lane in lanes)
    # The grid edge is at 4h/1h — never the 5m fee-wall band.
    assert all(lane.timeframe in ("4h", "1h") for lane in lanes)
    # Default strategy params so each lane matches exactly what the grid measured.
    assert all(lane.strategy_params == {} for lane in lanes)
    # The crown-jewel candidate (vnedge_algo_ml_pro ETH 4h) is enrolled.
    present = {(lane.strategy_id, lane.timeframe) for lane in lanes}
    assert ("vnedge_algo_ml_pro_v1", "4h") in present


def test_evidence_aligned_lanes_toggle_off():
    assert evidence_aligned_shadow_lanes({"MULTI_LANE_EVIDENCE_ALIGNED": "0"}) == []


def test_evidence_aligned_lanes_in_desired_specs():
    specs = desired_lane_specs({})
    identity = {(s.strategy_id, s.timeframe, s.exchange) for s in specs}
    assert ("vnedge_algo_ml_pro_v1", "4h", "delta_india") in identity


def test_lane_factory_builds_vnedge_algo_ml_pro():
    # vnedge_algo_ml_pro_v1 was previously unwired in the runtime; without the
    # new factory branch this call raises "unsupported lane strategy_id".
    strategy = _build_single_strategy("vnedge_algo_ml_pro_v1", {}, None, None)
    assert isinstance(strategy, VNEDGEAlgoMLProScanner)
