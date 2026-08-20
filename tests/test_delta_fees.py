"""Delta India fee model, verified against delta.exchange/fees on 2026-08-20.

Venue facts this pins:
  futures maker 0.02%, taker 0.05%
  18% GST is levied on the trading fee itself
  Scalper Offer waives the CLOSING leg inside 30 min for BTCUSD/ETHUSD and
  15 min for other futures; it needs KYC plus an explicit opt-in.
"""

from __future__ import annotations

import pytest

from vnedge.exchange.venue_specs import (
    scalper_offer_window_minutes,
    venue_fee_tax_mult,
    venue_taker_bps,
)


def test_delta_taker_carries_india_gst() -> None:
    """Delta fell through to the Binance default and was billed 5.0 flat.

    The venue bills 0.05% plus 18% GST, so every Delta lane was 18% too cheap.
    """
    assert venue_taker_bps("delta_india", include_tax=False) == pytest.approx(5.0)
    assert venue_taker_bps("delta_india") == pytest.approx(5.9)
    assert venue_fee_tax_mult("delta_india") == pytest.approx(1.18)


def test_venues_without_a_fee_tax_are_unchanged() -> None:
    assert venue_taker_bps("binanceusdm") == pytest.approx(5.0)
    assert venue_taker_bps("bybit") == pytest.approx(5.5)
    assert venue_fee_tax_mult("binanceusdm") == pytest.approx(1.0)


def test_scalper_window_is_shorter_for_altcoins() -> None:
    """A flat 30-minute rule hands altcoin lanes a discount they do not get."""
    assert scalper_offer_window_minutes("delta_india", "BTC/USD:USD") == 30.0
    assert scalper_offer_window_minutes("delta_india", "ETH/USD:USD") == 30.0
    for symbol in ("SOL/USD:USD", "XRP/USD:USD", "DOGE/USD:USD", "BNB/USD:USD"):
        assert scalper_offer_window_minutes("delta_india", symbol) == 15.0, symbol


def test_the_offer_does_not_exist_on_other_venues() -> None:
    assert scalper_offer_window_minutes("binanceusdm", "BTC/USDT:USDT") is None
    assert scalper_offer_window_minutes("bybit", "BTC/USDT:USDT") is None


def test_delta_round_trip_costs_what_the_venue_bills() -> None:
    """Taker both legs, no offer: 0.05% x 2 x 1.18 GST = 11.8 bps."""
    assert 2 * venue_taker_bps("delta_india") == pytest.approx(11.8)
