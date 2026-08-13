"""C3 audit fix: size_delta_risk_trade enforces the liquidation buffer like the
canonical position_sizer (a stop near liquidation is not a stop)."""
from vnedge.exchange.delta_contracts import DeltaContractSpec, size_delta_risk_trade

SPEC = DeltaContractSpec(symbol="ETHUSD", contract_value=0.01,
                         contract_unit_currency="ETH", maintenance_margin_pct=0.5)


def test_rejects_stop_too_close_to_liquidation():
    r = size_delta_risk_trade(
        account_equity_usd=800.0, risk_per_trade_pct=1.0,
        entry_price=100.0, stop_price=95.0, side="long",
        requested_leverage=30.0, acknowledge_high_leverage=True, spec=SPEC)
    assert not r.approved and "liquidation" in r.reason   # 30x liq ~2.8 < 6.0 buffered stop


def test_approves_when_stop_safely_inside_liquidation():
    r = size_delta_risk_trade(
        account_equity_usd=800.0, risk_per_trade_pct=1.0,
        entry_price=100.0, stop_price=99.0, side="long",
        requested_leverage=2.0, acknowledge_high_leverage=False, spec=SPEC)
    assert r.approved and r.liquidation_price is not None
