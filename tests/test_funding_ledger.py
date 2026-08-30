from __future__ import annotations

import pytest

from vnedge.paper.fill_model import FillModel
from vnedge.paper.simulated_exchange import PaperOrderRequest, SimulatedExchange
from vnedge.runtime.funding_ledger import FundingPrint, funding_cost_usd


def test_funding_cost_is_side_aware() -> None:
    assert funding_cost_usd(side="long", notional_usd=1_000, rate=0.0001) == pytest.approx(0.1)
    assert funding_cost_usd(side="short", notional_usd=1_000, rate=0.0001) == pytest.approx(-0.1)


def test_flat_book_ignores_settlement_and_never_retroactively_charges_it() -> None:
    exchange = SimulatedExchange(FillModel(slippage_bps=0, taker_fee_bps=0), 1_000)
    event = FundingPrint(ts_ms=1_750_000_000_000, rate=0.0001)
    assert exchange.apply_funding_print("BTCUSD", event) is None
    exchange.set_quote("BTCUSD", 100, 100)
    exchange.submit_order(PaperOrderRequest("open", "BTCUSD", True, 1))
    assert exchange.apply_funding_print("BTCUSD", event) is None
    assert exchange.balance_usd == pytest.approx(1_000)


def test_paper_long_is_debited_once_and_short_is_credited() -> None:
    long_book = SimulatedExchange(FillModel(slippage_bps=0, taker_fee_bps=0), 1_000)
    long_book.set_quote("BTCUSD", 100, 100)
    long_book.submit_order(PaperOrderRequest("long", "BTCUSD", True, 1))
    event = FundingPrint(ts_ms=1_750_000_000_000, rate=0.001)
    booked = long_book.apply_funding_print("BTCUSD", event)
    assert booked is not None
    assert booked.funding_cost_usd == pytest.approx(0.1)
    assert long_book.balance_usd == pytest.approx(999.9)
    assert long_book.apply_funding_print("BTCUSD", event) is None

    short_book = SimulatedExchange(FillModel(slippage_bps=0, taker_fee_bps=0), 1_000)
    short_book.set_quote("BTCUSD", 100, 100)
    short_book.submit_order(PaperOrderRequest("short", "BTCUSD", False, 1))
    short_book.apply_funding_print("BTCUSD", event)
    assert short_book.balance_usd == pytest.approx(1_000.1)
