"""B1 audit fix: the backtest FeeModel/SlippageModel and paper FillModel default
their fee/slip from plan.cost_model — one source, so research/paper/live can't
silently drift apart on costs."""
from vnedge.backtest.fee_model import FeeModel
from vnedge.backtest.slippage_model import SlippageModel
from vnedge.paper.fill_model import FillModel
from vnedge.plan.cost_model import (
    COST_PROFILES,
    DEFAULT_MAKER_FEE_BPS,
    DEFAULT_SLIP_BPS,
    DEFAULT_TAKER_FEE_BPS,
    CostModelConfig,
)
from vnedge.risk.cost_gate import CostGate, CostProfile


def test_defaults_agree_with_cost_model():
    cm = CostModelConfig()
    assert DEFAULT_TAKER_FEE_BPS == cm.taker_fee_bps
    assert DEFAULT_MAKER_FEE_BPS == cm.maker_fee_bps
    assert DEFAULT_SLIP_BPS == cm.default_slip_entry_bps == cm.default_slip_exit_bps


def test_fee_models_source_from_cost_model():
    assert FeeModel().taker_bps == DEFAULT_TAKER_FEE_BPS
    assert FeeModel().maker_bps == DEFAULT_MAKER_FEE_BPS
    assert SlippageModel().bps == DEFAULT_SLIP_BPS
    assert FillModel().taker_fee_bps == DEFAULT_TAKER_FEE_BPS
    assert FillModel().maker_fee_bps == DEFAULT_MAKER_FEE_BPS
    assert FillModel().slippage_bps == DEFAULT_SLIP_BPS


def test_cost_gate_reads_the_canonical_profile_table():
    scalp = COST_PROFILES["scalp"]
    result = CostGate(CostProfile.SCALP).evaluate(
        signal_edge_bps=100,
        side="buy",
        urgency="taker",
        expected_holding_seconds=0,
        current_funding_rate=0,
        symbol="BTCUSDT",
    )
    expected = 2 * scalp.taker_fee_bps + (
        scalp.default_slip_entry_bps + scalp.default_slip_exit_bps
    )
    assert float(result.cost.total_cost_bps) == expected
