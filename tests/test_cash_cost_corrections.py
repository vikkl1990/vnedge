from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from vnedge.backtest.backtester import BacktestConfig
from vnedge.backtest.fee_model import FeeModel
from vnedge.execution.evidence import CostDecisionEvidence
from vnedge.paper.fill_model import FillModel
from vnedge.plan.cash_costs import ChargedFill, contracts_to_base, fee_cash, reconcile_cash_fills
from vnedge.plan.cost_model import CostModel
from vnedge.risk.cost_gate import CostGate, CostProfile
from vnedge.risk.fee_model import FeeSchedule, ScalperOfferRule
from vnedge.runtime.scanner_session import SessionCosts


@pytest.mark.parametrize("maker", [False, True])
def test_v2_gate_model_session_share_conservative_cost(maker):
    model = CostModel.for_profile("delta_scalp_v2")
    gate = CostGate(CostProfile.DELTA_SCALP_V2).evaluate(
        100, "buy", "maker" if maker else "taker", 60, "BTCUSD",
    ).cost
    session = SessionCosts.from_profile("delta_scalp_v2")
    expected = D("14.26") if maker else D("17.8")
    assert gate.estimated_execution_bps == expected
    assert gate.total_cost_bps == gate.booked_execution_bps == expected
    assert model.round_trip_bps(maker_entry=maker, include_safety=False) == pytest.approx(float(expected))
    assert session.round_trip_bps(1, maker_entry=maker) == pytest.approx(float(expected))
    assert float(gate.gate_cost_bps) == pytest.approx(model.round_trip_bps(maker_entry=maker))
    assert gate.approval_gross_floor_bps == expected + 4
    assert gate.cost_basis == "pretrade_estimate"
    assert gate.cost_profile_id == "delta_scalp_v2"
    assert gate.cost_config_sha256 == model.config_sha256


def test_cost_attribution_survives_the_journal_evidence_boundary():
    result = CostGate(CostProfile.DELTA_SCALP_V2).evaluate(100, "buy", "maker", 60, "BTCUSD")
    snapshot = CostDecisionEvidence.from_result(result, profile="delta_scalp_v2")
    row = snapshot.as_dict()
    assert row["cost_basis"] == "pretrade_estimate"
    assert row["cost_config_sha256"] == result.cost.cost_config_sha256
    assert row["cost_profile_id"] == "delta_scalp_v2"
    assert D(row["estimated_execution_bps"]) == D("14.26")
    assert CostDecisionEvidence(**row).as_dict() == row
    assert "cost_basis" not in CostDecisionEvidence.not_evaluated().as_dict()


def test_legacy_goldens_remain_frozen_and_v2_does_not_weaken_gate():
    legacy = CostModel.for_profile("delta_scalp")
    new = CostModel.for_profile("delta_scalp_v2")
    assert legacy.round_trip_bps(maker_entry=True, include_safety=False) == pytest.approx(14.26)
    old_gate = CostGate(CostProfile.DELTA_SCALP).evaluate(17, "buy", "maker", 60, "BTCUSD")
    new_gate = CostGate(CostProfile.DELTA_SCALP_V2).evaluate(17, "buy", "maker", 60, "BTCUSD")
    assert old_gate.cost.total_cost_bps == D("12.76") and old_gate.approved
    assert not new_gate.approved
    assert legacy.config_sha256 != new.config_sha256
    assert new.config.free_exit_within_minutes is None
    cfg = BacktestConfig(cost_profile="delta_scalp_v2")
    assert cfg.fees.taker_bps == pytest.approx(5.9)
    assert cfg.slippage.bps == 3


def test_cash_fee_shared_without_second_gst_or_leverage_multiplier():
    base = contracts_to_base(10, "0.001")
    assert base == D("0.010")
    assert fee_cash(base * D(100000), "5.9") == D("0.59")
    assert FeeModel(taker_bps=5.9).taker_fee_usd(1002) == float(D("0.59118"))
    assert FillModel(taker_fee_bps=5.9).fee_usd(1002) == float(D("0.59118"))


def fill(fill_id="open", **kwargs):
    return ChargedFill(**({
        "fill_id": fill_id, "venue": "delta_india", "account_id": "fixture-subaccount",
        "symbol": "BTCUSD", "decision_id": "fixture-decision", "quote_currency": "USD",
        "charge_currency": "USD", "side": "buy", "leg": "open", "quantity_base": D("0.01"),
        "price": D(100000), "fee_paid": D("0.59"), "fee_includes_tax": True, "tax_paid": None,
        "source_ref": "fixture-only", "basis": "simulated",
    } | kwargs))


def close(**kwargs):
    return fill("close", **({"side": "sell", "leg": "close", "price": D(100200), "fee_paid": D("0.59118")} | kwargs))


def test_actual_price_pnl_has_no_second_spread_slippage_or_tax():
    result = reconcile_cash_fills([fill(), close()], funding_paid=D(0))
    assert result.gross_cash == D(2)
    assert result.charges_cash == D("1.18118")
    assert result.net_cash == D("0.81882")
    assert result.net_bps == D("8.1882")
    split_tax = replace(fill(), fee_paid=D("0.5"), fee_includes_tax=False, tax_paid=D("0.09"))
    assert reconcile_cash_fills([split_tax, close()], funding_paid=D(0)) == result


def test_partial_fills_dedupe_and_charge_each_executed_notional():
    a = fill(quantity_base=D("0.004"), fee_paid=D("0.236"))
    b = fill("open2", quantity_base=D("0.006"), fee_paid=D("0.354"))
    c = close(quantity_base=D("0.003"), fee_paid=D("0.177354"))
    d = replace(c, fill_id="close2", quantity_base=D("0.007"), fee_paid=D("0.413826"))
    result = reconcile_cash_fills([a, b, c, a, d], funding_paid=D("-0.1"))
    assert result.fill_count == 4
    assert result.net_cash == D("0.91882")
    assert result.entry_notional == D(1000)


def test_short_and_reported_rebate_and_unknown_funding():
    a = fill(side="sell", fee_paid=D("-0.1"))
    b = close(side="buy", price=D(99800), fee_paid=D("0.58882"))
    result = reconcile_cash_fills([a, b], funding_paid=None)
    assert result.gross_cash == 2
    assert result.net_before_funding == D("1.51118")
    assert result.net_cash is None and result.net_bps is None


@pytest.mark.parametrize("kwargs", [
    {"quantity_base": 0}, {"quantity_base": "NaN"}, {"price": "Infinity"},
    {"fee_paid": None}, {"fee_paid": True}, {"fee_includes_tax": "true"},
    {"tax_paid": D("0.09")}, {"fee_includes_tax": False},
    {"charge_currency": "INR"}, {"source_ref": ""}, {"basis": "estimated"},
])
def test_incomplete_or_ambiguous_cash_evidence_rejected(kwargs):
    with pytest.raises((ValueError, ArithmeticError, TypeError)):
        fill(**kwargs)


@pytest.mark.parametrize("rows", [
    [], [fill()], [close(), fill()],
    [fill(), close(quantity_base=D("0.02"))],
    [fill(), replace(fill(), price=D(90000)), close()],
    [fill(), close(account_id="other")], [fill(), close(decision_id="other")],
    [fill(), close(symbol="ETHUSD")], [fill(), close(basis="venue_reported")],
    [fill(), close(side="buy")],
])
def test_bad_round_trips_rejected(rows):
    with pytest.raises(ValueError):
        reconcile_cash_fills(rows, funding_paid=D(0))


def test_verified_waiver_cash_is_separate_from_full_tariff_scenario():
    now = datetime(2026, 9, 11, tzinfo=UTC)
    base = FeeSchedule(taker_bps=D(5), maker_bps=D(2), gst=D("0.18"))
    verified = replace(
        base, account_verified=True, verification_id="fixture-account-consent",
        discounts_verified=True, discounts_verified_until=now + timedelta(hours=1),
        scalper_offer_active=True, scalper_consent=True,
        scalper_rules=(ScalperOfferRule("BTCUSD", 1800),), schedule_id="fixture-only-v2",
    )
    def charge(schedule, hold=60, at=now):
        return schedule.calculation("taker", leg="close", symbol="BTCUSD", hold_seconds=hold, as_of=at).estimated_cash(D(1002))
    assert charge(base) == D("0.59118")
    assert charge(verified) == 0
    assert charge(verified, hold=1801) == D("0.59118")
    assert charge(replace(verified, scalper_consent=False)) == D("0.59118")
    assert charge(verified, at=now + timedelta(hours=2)) == D("0.59118")
    assert charge(verified, at=None) == D("0.59118")
    assert verified.calculation(
        "taker", leg="close", symbol="BTCUSD", hold_seconds=60,
    ).fallback_reason == "discount_evaluation_time_missing"


def test_statement_audit_is_read_only_and_does_not_claim_account_verification(tmp_path, capsys):
    import hashlib
    import json
    from dataclasses import asdict

    from vnedge.plan.cash_costs import main

    path = tmp_path / "statement.json"
    raw = json.dumps({
        "fills": [asdict(fill()), asdict(close())],
        "funding_paid": "0", "funding_source_ref": "fixture-only-no-funding",
    }, default=str).encode()
    path.write_bytes(raw)
    main([str(path)])
    output = json.loads(capsys.readouterr().out)
    assert output["net_cash"] == "0.81882"
    assert output["input_sha256"] == hashlib.sha256(raw).hexdigest()
    assert output["read_only"] and not output["account_statement_verified"]
    assert path.read_bytes() == raw
