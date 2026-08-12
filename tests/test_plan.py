import pytest

from vnedge.plan import (
    AISpec, CostModel, CostSpec, EntryEngine, EntrySpec, ExitEngine,
    ProfitSpec, RiskSpec, Target, TradePlan, plan_gate,
)


def _plan(*, side="long", expected_net_bps=30.0, targets=None, stop_bps=40.0,
          trail=None, time_stop=100, entry=None):
    targets = targets or (Target(60.0, 50.0), Target(120.0, 50.0))
    return TradePlan(
        side=side, decision_tf="1h",
        entry=entry or EntrySpec(),
        risk=RiskSpec(stop_bps=stop_bps),
        profit=ProfitSpec(targets=targets, time_stop_bars=time_stop, trail_bps=trail),
        costs=CostSpec(fee_entry_bps=5, fee_exit_bps=5, slip_entry_bps=2, slip_exit_bps=2),
        ai=AISpec(expected_net_bps=expected_net_bps),
        source="test",
    )


# --- CostModel ---------------------------------------------------------------
def test_cost_model_math():
    cm = CostModel()
    assert cm.round_trip_bps() == 17.0                 # 5+5+2+2+3(safety)
    assert cm.net_bps(30.0) == 13.0
    assert cm.round_trip_bps(funding_bps=4.0) == 21.0
    assert cm.round_trip_bps(maker_entry=True) == 14.0  # 2+5+2+2+3


# --- plan_gate (the hard cost gate) ------------------------------------------
def test_gate_rejects_nonpositive_net():
    ok, reasons = plan_gate(_plan(expected_net_bps=-1.0), CostModel())
    assert not ok and any("expected_net_bps" in r for r in reasons)


def test_gate_rejects_tp1_below_cost_multiple():
    ok, reasons = plan_gate(
        _plan(targets=(Target(20.0, 50.0), Target(40.0, 50.0))), CostModel())
    assert not ok and any("TP1" in r for r in reasons)   # 20bps < 2×17


def test_gate_accepts_valid_plan():
    ok, reasons = plan_gate(_plan(expected_net_bps=15.0), CostModel())
    assert ok and reasons == []


# --- TradePlan validation ----------------------------------------------------
def test_plan_rejects_stopless():
    with pytest.raises(ValueError):
        _plan(stop_bps=0.0)


def test_plan_rejects_oversized_ladder():
    with pytest.raises(ValueError):
        _plan(targets=(Target(60, 70.0), Target(120, 50.0)))   # 120% > 100%


# --- EntryEngine -------------------------------------------------------------
def test_entry_next_open_fill_and_slip_reject():
    eng = EntryEngine()
    p = _plan(entry=EntrySpec(type="next_open", max_entry_slip_bps=5.0))
    r = eng.evaluate(p, 100.0, next_open=100.02)          # +2bps adverse
    assert r.status == "filled" and abs(r.slippage_bps - 2.0) < 0.1
    assert eng.evaluate(p, 100.0, next_open=100.10).status == "reject"   # +10bps


def test_entry_limit_fill_timeout_waiting():
    eng = EntryEngine()
    p = _plan(entry=EntrySpec(type="limit_bps", limit_offset_bps=10.0, entry_timeout_bars=2))
    assert eng.evaluate(p, 100.0, current_price=99.85).status == "filled"   # ≤ 99.90 limit
    assert eng.evaluate(p, 100.0, current_price=100.5, bars_elapsed=2).status == "timeout"
    assert eng.evaluate(p, 100.0, current_price=100.5, bars_elapsed=0).status == "waiting"


def test_entry_ltf_confirm():
    eng = EntryEngine()
    p = _plan(entry=EntrySpec(type="ltf_confirm", entry_timeout_bars=3, max_entry_slip_bps=50))
    assert eng.evaluate(p, 100.0, current_price=100.1, ltf_confirmed=True).status == "filled"
    assert eng.evaluate(p, 100.0, ltf_confirmed=False, bars_elapsed=3).status == "timeout"


# --- ExitEngine --------------------------------------------------------------
def test_exit_stop_wins_ties():
    ee = ExitEngine(_plan(), entry_price=100.0)          # stop 99.60, tp1 100.60
    ev = ee.on_bar(high=100.7, low=99.5, close=100.0, bars_in_trade=1)
    assert len(ev) == 1 and ev[0].reason == "stop" and ev[0].action == "close"
    assert ee.closed


def test_exit_tp_ladder_and_breakeven_ratchet():
    ee = ExitEngine(_plan(), entry_price=100.0)          # tp1 100.60, tp2 101.20
    ev = ee.on_bar(high=100.65, low=100.0, close=100.5, bars_in_trade=1)
    assert any(e.reason == "tp1" and e.action == "partial" for e in ev)
    assert abs(ee.remaining - 0.5) < 1e-9
    assert abs(ee.stop - 100.0) < 1e-9                   # stop ratcheted to breakeven
    ev2 = ee.on_bar(high=101.25, low=100.5, close=101.0, bars_in_trade=2)
    assert any(e.reason == "tp2" and e.action == "close" for e in ev2)
    assert ee.closed


def test_exit_time_stop_closes_residual():
    ee = ExitEngine(_plan(stop_bps=200.0, targets=(Target(500, 50.0), Target(1000, 50.0)),
                          time_stop=3), entry_price=100.0)
    ee.on_bar(101.0, 99.5, 100.5, 1)
    ee.on_bar(101.0, 99.5, 100.5, 2)
    ev = ee.on_bar(101.0, 99.5, 100.5, 3)
    assert any(e.reason == "time_stop" and e.action == "close" for e in ev)
    assert ee.closed
