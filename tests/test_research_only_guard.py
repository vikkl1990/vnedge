"""Capital eligibility is an allowlist, never an unknown-ID default."""
from vnedge.runtime.multi_lane import LaneSpec
from vnedge.runtime.runner_config import RunnerMode
from vnedge.strategy.strategy_registry import (
    CAPITAL_APPROVED,
    RESEARCH_ONLY,
    SHADOW_OBSERVE,
    capital_denial_reason,
    is_capital_eligible,
    is_shadow_observe_eligible,
)


def _spec(strategy_id, mode):
    return LaneSpec(lane_id="t", exchange="binanceusdm", symbol="BTC/USDT:USDT",
                    mode=mode, strategy_id=strategy_id)


def test_measurement_runtime_is_explicitly_non_capital():
    assert RESEARCH_ONLY == {
        "measurement_only_v1",
        "range_expansion_observer_v1",
        "range_expansion_observer_v2",
        "range_expansion_observer_v3",
        "range_expansion_observer_v4",
        "structure_bos_1h",
        "structure_bos_15m_trigger_v2",
        "structure_bos_15m_trigger_v3",
        "fee_wall_momentum_observer_v1",
        "squeeze_expansion_breakout_v2",
        "squeeze_expansion_breakout_v3",
        "squeeze_expansion_breakout_v4",
    }
    for sid in RESEARCH_ONLY:
        assert not is_capital_eligible(sid)


def test_capital_permission_requires_an_explicit_promotion():
    assert CAPITAL_APPROVED == frozenset()
    assert not is_capital_eligible("crypto_trend_atr_margin_v1")
    assert capital_denial_reason("crypto_trend_atr_margin_v1") == (
        "strategy has no explicit capital approval"
    )
    assert capital_denial_reason("funding_mean_reversion_v1") == "strategy is killed"
    assert capital_denial_reason("measurement_only_v1") == (
        "strategy is research/measurement only"
    )
    assert capital_denial_reason("structure_bos_1h") == (
        "strategy is research/measurement only"
    )
    assert capital_denial_reason("fee_wall_momentum_observer_v1") == (
        "strategy is research/measurement only"
    )
    # funding_mr FAILED forward paper (2026-08-14) -> post-mortem KILLED, capital
    # permission revoked (see docs/EDGE_INVESTIGATION_POSTMORTEM_20260816).
    assert not is_capital_eligible("funding_mean_reversion_v1")
    assert not is_capital_eligible("unknown_or_removed_scanner")


def test_shadow_observe_is_a_separate_narrow_permission():
    assert SHADOW_OBSERVE == {
        "structure_bos_1h",
        "range_expansion_observer_v1",
        "range_expansion_observer_v2",
        "range_expansion_observer_v3",
        "range_expansion_observer_v4",
        "structure_bos_15m_trigger_v2",
        "structure_bos_15m_trigger_v3",
        "fee_wall_momentum_observer_v1",
        "squeeze_expansion_breakout_v2",
        "squeeze_expansion_breakout_v3",
        "squeeze_expansion_breakout_v4",
    }
    assert is_shadow_observe_eligible("squeeze_expansion_breakout_v2")
    assert is_shadow_observe_eligible("squeeze_expansion_breakout_v3")
    assert is_shadow_observe_eligible("squeeze_expansion_breakout_v4")
    assert is_shadow_observe_eligible("range_expansion_observer_v1")
    assert is_shadow_observe_eligible("range_expansion_observer_v3")
    assert is_shadow_observe_eligible("range_expansion_observer_v4")
    assert is_shadow_observe_eligible("structure_bos_1h")
    assert is_shadow_observe_eligible("structure_bos_15m_trigger_v2")
    assert is_shadow_observe_eligible("structure_bos_15m_trigger_v3")
    assert is_shadow_observe_eligible("fee_wall_momentum_observer_v1")
    assert not is_capital_eligible("fee_wall_momentum_observer_v1")
    assert not is_capital_eligible("structure_bos_1h")
    assert not is_shadow_observe_eligible("funding_mean_reversion_v1")
    assert not is_shadow_observe_eligible("unknown_or_removed_scanner")


def test_paper_lane_for_research_only_is_downgraded_to_shadow():
    spec = _spec(next(iter(RESEARCH_ONLY)), RunnerMode.PAPER).capital_downgraded()
    assert spec.mode is RunnerMode.SHADOW           # capital denied, observation kept


def test_shadow_lane_for_research_only_is_untouched():
    spec = _spec(next(iter(RESEARCH_ONLY)), RunnerMode.SHADOW).capital_downgraded()
    assert spec.mode is RunnerMode.SHADOW


def test_paper_lane_for_unapproved_survivor_is_downgraded():
    spec = _spec("crypto_trend_atr_margin_v1", RunnerMode.PAPER).capital_downgraded()
    assert spec.mode is RunnerMode.SHADOW


def test_paper_lane_for_killed_strategy_is_downgraded_to_shadow():
    # funding_mr is post-mortem KILLED: capital denied, observation kept.
    spec = _spec("funding_mean_reversion_v1", RunnerMode.PAPER).capital_downgraded()
    assert spec.mode is RunnerMode.SHADOW


def test_downgrade_preserves_all_other_fields():
    spec = _spec(next(iter(RESEARCH_ONLY)), RunnerMode.PAPER)
    down = spec.capital_downgraded()
    assert down.lane_id == spec.lane_id and down.symbol == spec.symbol
    assert down.strategy_id == spec.strategy_id     # only mode changed
