"""Evidence-aligned Delta shadow roster (second_eye_grid backtest).

Guards the roster realignment: the grid-proven 4h/1h survivors are enrolled as
Delta-India SHADOW lanes, ``desired_lane_specs`` includes them, and the lane
factory can construct ``vnedge_algo_ml_pro`` — previously unwired in the runtime,
so a lane for it would have raised ``unsupported lane strategy_id``.
"""

from pathlib import Path

from vnedge.runtime.multi_lane import _build_single_strategy
from vnedge.runtime.multi_lane_shadow import (
    EVIDENCE_ALIGNED_DELTA_LANES,
    EVIDENCE_PAPER_TRIAL_LANES,
    desired_lane_specs,
    evidence_aligned_shadow_lanes,
    evidence_paper_trial_lanes,
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


def test_paper_trial_lanes_run_the_candidate_in_paper_mode():
    lanes = evidence_paper_trial_lanes({})

    assert len(lanes) == len(EVIDENCE_PAPER_TRIAL_LANES)
    # Real-money-equivalent forward test => PAPER (simulated fills), not SHADOW.
    assert all(lane.mode is RunnerMode.PAPER for lane in lanes)
    assert all(lane.exchange == "delta_india" for lane in lanes)
    assert all(lane.strategy_id == "vnedge_algo_ml_pro_v1" for lane in lanes)
    # The crown jewel (ETH 4h) is the trial's primary lane.
    present = {(lane.symbol, lane.timeframe) for lane in lanes}
    assert ("ETH/USD:USD", "4h") in present


def test_paper_trial_lanes_toggle_off():
    assert evidence_paper_trial_lanes({"MULTI_LANE_EVIDENCE_PAPER_TRIAL": "0"}) == []


def test_paper_trial_lanes_in_desired_specs():
    specs = desired_lane_specs({})
    paper = {
        (s.strategy_id, s.timeframe)
        for s in specs
        if s.mode is RunnerMode.PAPER and s.strategy_id == "vnedge_algo_ml_pro_v1"
    }
    assert ("vnedge_algo_ml_pro_v1", "4h") in paper


def test_pre_registered_trial_manifest_exists_and_is_paper_only():
    import yaml

    manifest = Path("research/paper_trials/vnedge_algo_ml_pro_eth_4h_20260726.yaml")
    assert manifest.exists()
    data = yaml.safe_load(manifest.read_text())
    assert data["strategy"] == "vnedge_algo_ml_pro_v1"
    assert data["live_orders_enabled"] is False
    assert data["approved_by"] == "human"
    assert data["strategy_params"] == {}  # default config, matches the grid
