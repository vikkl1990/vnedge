"""Scalp/swing cost worlds — one CostModel per lane profile, one gate story."""
import pytest

from vnedge.plan import (
    AISpec, CostModel, CostSpec, EntrySpec, ProfitSpec, RiskSpec, Target,
    TradePlan, plan_gate,
)
from vnedge.plan.cost_model import COST_PROFILES


def test_profiles_registered():
    assert set(COST_PROFILES) == {"swing", "scalp", "delta_scalp"}


def test_for_profile_and_unknown():
    assert CostModel.for_profile("scalp").profile == "scalp"
    with pytest.raises(ValueError):
        CostModel.for_profile("nope")


def test_gate_multiple_is_profile_specific():
    assert CostModel.for_profile("swing").config.gate_safety_mult == 2.0
    assert CostModel.for_profile("scalp").config.gate_safety_mult == 3.0
    assert CostModel.for_profile("delta_scalp").config.gate_safety_mult == 3.5


def test_delta_scalp_does_not_assume_unverified_close_waiver():
    cm = CostModel.for_profile("delta_scalp")
    full = cm.round_trip_bps()
    within = cm.round_trip_bps(hold_minutes=10)
    beyond = cm.round_trip_bps(hold_minutes=45)
    assert within == full
    assert beyond == full
    assert cm.round_trip_bps() == full


def test_swing_has_no_free_exit_window():
    cm = CostModel.for_profile("swing")
    assert cm.round_trip_bps(hold_minutes=1) == cm.round_trip_bps()


def _plan(tp1_bps, *, cfg):
    return TradePlan(
        side="long", decision_tf="1h", entry=EntrySpec(),
        risk=RiskSpec(stop_bps=40.0),
        profit=ProfitSpec(targets=(Target(tp1_bps, 100.0),), time_stop_bars=48),
        costs=CostSpec(fee_entry_bps=5, fee_exit_bps=5, slip_entry_bps=2, slip_exit_bps=2),
        ai=AISpec(expected_net_bps=tp1_bps - 17.0), source="t")


def test_plan_gate_uses_profile_multiple_when_none():
    # full_rt with default CostSpec + safety 3 = 17; scalp needs 3x=51, swing 2x=34.
    p = _plan(45.0, cfg=None)              # 45 bps TP1
    ok_swing, _ = plan_gate(p, CostModel.for_profile("swing"))   # 45 >= 2*17=34 -> ok
    ok_scalp, reasons = plan_gate(p, CostModel.for_profile("scalp"))  # 45 < 3*17=51 -> no
    assert ok_swing is True
    assert ok_scalp is False and any("TP1" in r for r in reasons)


def test_explicit_safety_mult_still_overrides_profile():
    p = _plan(45.0, cfg=None)
    ok, _ = plan_gate(p, CostModel.for_profile("scalp"), safety_mult=2.0)
    assert ok is True                     # explicit arg wins over the profile default


def test_default_cost_model_unchanged():
    # back-compat: bare CostModel() is the swing world, 2x gate, 17bps round-trip
    cm = CostModel()
    assert cm.config.gate_safety_mult == 2.0
    assert cm.round_trip_bps() == 17.0
    assert cm.profile == "swing"
