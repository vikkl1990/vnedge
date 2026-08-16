"""Capital-eligibility guard: research-only scanner families run SHADOW for
observation but are downgraded from any PAPER capital lane at roster build, so a
single roster edit (or a stale governor proposal) can never deploy the over-fit
scanner zoo with capital."""
from vnedge.runtime.multi_lane import LaneSpec
from vnedge.runtime.runner_config import RunnerMode
from vnedge.strategy.strategy_registry import RESEARCH_ONLY, is_capital_eligible


def _spec(strategy_id, mode):
    return LaneSpec(lane_id="t", exchange="binanceusdm", symbol="BTC/USDT:USDT",
                    mode=mode, strategy_id=strategy_id)


def test_research_only_set_is_the_named_families():
    # FVG + Luxara×3 + confluence×4 = 8 structurally over-fit families
    assert len(RESEARCH_ONLY) == 8
    for sid in RESEARCH_ONLY:
        assert not is_capital_eligible(sid)


def test_survivors_stay_capital_eligible():
    # crypto_trend is immature (few trades) but NOT killed -> keeps capital permission
    # so a future swing-timescale validation can promote it.
    assert is_capital_eligible("crypto_trend_atr_margin_v1")
    # funding_mr FAILED forward paper (2026-08-14) -> post-mortem KILLED, capital
    # permission revoked (see docs/EDGE_INVESTIGATION_POSTMORTEM_20260816).
    assert not is_capital_eligible("funding_mean_reversion_v1")


def test_paper_lane_for_research_only_is_downgraded_to_shadow():
    spec = _spec(next(iter(RESEARCH_ONLY)), RunnerMode.PAPER).capital_downgraded()
    assert spec.mode is RunnerMode.SHADOW           # capital denied, observation kept


def test_shadow_lane_for_research_only_is_untouched():
    spec = _spec(next(iter(RESEARCH_ONLY)), RunnerMode.SHADOW).capital_downgraded()
    assert spec.mode is RunnerMode.SHADOW


def test_paper_lane_for_survivor_is_untouched():
    spec = _spec("crypto_trend_atr_margin_v1", RunnerMode.PAPER).capital_downgraded()
    assert spec.mode is RunnerMode.PAPER            # survivors keep capital


def test_paper_lane_for_killed_strategy_is_downgraded_to_shadow():
    # funding_mr is post-mortem KILLED: capital denied, observation kept.
    spec = _spec("funding_mean_reversion_v1", RunnerMode.PAPER).capital_downgraded()
    assert spec.mode is RunnerMode.SHADOW


def test_downgrade_preserves_all_other_fields():
    spec = _spec(next(iter(RESEARCH_ONLY)), RunnerMode.PAPER)
    down = spec.capital_downgraded()
    assert down.lane_id == spec.lane_id and down.symbol == spec.symbol
    assert down.strategy_id == spec.strategy_id     # only mode changed
