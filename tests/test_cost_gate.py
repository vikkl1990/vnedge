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


def test_room_multiple_fails_closed_when_room_is_missing_or_too_small():
    gate = CostGate(
        CostProfile.SCALP,
        min_net_edge_bps=Decimal("4"),
        min_room_cost_multiple=Decimal("1.5"),
    )
    common = dict(
        signal_edge_bps=100,
        side="buy",
        urgency="taker",
        expected_holding_seconds=0,
        current_funding_rate=0,
        symbol="BTCUSDT",
    )

    missing = gate.evaluate(**common)
    thin = gate.evaluate(**common, available_room_bps=Decimal("20"))

    assert not missing.approved and "room missing" in (missing.reason or "")
    assert not thin.approved and "room 20.00bps" in (thin.reason or "")
    assert thin.min_room_bps == Decimal("21.0")


def test_room_multiple_approves_only_after_net_and_room_checks_pass():
    gate = CostGate(
        CostProfile.SCALP,
        min_net_edge_bps=Decimal("4"),
        min_room_cost_multiple=Decimal("1.5"),
    )
    result = gate.evaluate(
        signal_edge_bps=100,
        side="buy",
        urgency="taker",
        expected_holding_seconds=0,
        current_funding_rate=0,
        symbol="BTCUSDT",
        available_room_bps=Decimal("21"),
    )

    assert result.approved
    assert result.available_room_bps == Decimal("21")
    assert result.min_room_bps == Decimal("21.0")


def test_funding_rebate_cannot_reduce_the_physical_room_wall():
    gate = CostGate(
        CostProfile.SCALP,
        min_room_cost_multiple=Decimal("1.5"),
    )
    result = gate.evaluate(
        signal_edge_bps=100,
        side="sell",
        urgency="taker",
        expected_holding_seconds=28_800,
        current_funding_rate="0.001",
        symbol="BTCUSDT",
        available_room_bps=Decimal("10"),
    )

    assert result.cost.funding_bps == Decimal("-10")
    assert result.min_room_bps == Decimal("21.0")
    assert not result.approved
