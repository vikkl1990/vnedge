"""B1 audit fix: the backtest FeeModel/SlippageModel and paper FillModel default
their fee/slip from plan.cost_model — one source, so research/paper/live can't
silently drift apart on costs."""
from vnedge.backtest.fee_model import FeeModel
from vnedge.backtest.slippage_model import SlippageModel
from vnedge.paper.fill_model import FillModel
from vnedge.plan.cost_model import (
    DEFAULT_MAKER_FEE_BPS, DEFAULT_SLIP_BPS, DEFAULT_TAKER_FEE_BPS, CostModelConfig,
)


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
