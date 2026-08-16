"""CostGate — the hard, pre-sizing cost filter for the HF path."""

from decimal import Decimal

import pytest

from vnedge.risk.cost_gate import CostGate, CostProfile


def _gate(profile=CostProfile.SCALP, min_net="4.0"):
    return CostGate(profile, min_net_edge_bps=Decimal(min_net))


def test_weak_edge_is_rejected_with_reason():
    # a 1bps edge cannot cover the ~14bps taker round-trip + 4bps min net
    r = _gate().evaluate(signal_edge_bps=1, side="buy", urgency="taker",
                         expected_holding_seconds=0, current_funding_rate=0, symbol="BTCUSDT")
    assert r.approved is False and r.reason is not None
    assert r.cost.total_cost_bps == Decimal("14")   # 10 fee + 4 slip


def test_strong_maker_edge_approved():
    r = _gate().evaluate(signal_edge_bps=15, side="buy", urgency="maker",
                         expected_holding_seconds=0, current_funding_rate=0, symbol="BTCUSDT")
    assert r.approved is True and r.reason is None
    assert r.cost.total_cost_bps == Decimal("10")   # 7 fee (2 maker + 5 taker exit) + 3 slip
    assert r.expected_net_bps == Decimal("5")


def test_maker_is_cheaper_than_taker():
    common = dict(signal_edge_bps=100, side="buy", expected_holding_seconds=0,
                  current_funding_rate=0, symbol="BTCUSDT")
    m = _gate().evaluate(urgency="maker", **common)
    t = _gate().evaluate(urgency="taker", **common)
    assert m.cost.total_cost_bps < t.cost.total_cost_bps


def test_funding_long_pays_short_earns():
    common = dict(signal_edge_bps=100, urgency="taker", expected_holding_seconds=28800,
                  current_funding_rate="0.0001", symbol="BTCUSDT")   # 1bp/8h over one interval
    long = _gate().evaluate(side="buy", **common)
    short = _gate().evaluate(side="sell", **common)
    assert long.cost.funding_bps == Decimal("1")     # long pays funding
    assert short.cost.funding_bps == Decimal("-1")   # short earns it
    assert long.cost.total_cost_bps > short.cost.total_cost_bps


def test_delta_india_costs_more_than_binance():
    common = dict(signal_edge_bps=100, side="buy", urgency="taker",
                  expected_holding_seconds=0, current_funding_rate=0, symbol="BTCUSDT")
    binance = CostGate(CostProfile.SCALP).evaluate(**common)
    delta = CostGate(CostProfile.DELTA_SCALP).evaluate(**common)
    assert delta.cost.total_cost_bps > binance.cost.total_cost_bps   # 18% GST + thinner book


def test_result_is_frozen():
    r = _gate().evaluate(signal_edge_bps=1, side="buy", urgency="taker",
                         expected_holding_seconds=0, current_funding_rate=0, symbol="X")
    with pytest.raises(Exception):
        r.approved = True   # frozen pydantic model


def test_threshold_immutable_at_runtime():
    g = _gate()
    with pytest.raises(Exception):
        g.min_net_edge_bps = Decimal("0")        # property, no setter
    with pytest.raises(Exception):
        g.config.min_net_edge_bps = Decimal("0")  # frozen config
