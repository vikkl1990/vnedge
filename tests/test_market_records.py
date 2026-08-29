from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from vnedge.data.market_records import LaneBBO, PublicTrade
from vnedge.exchange.live_feed import QuoteUpdate

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)


def test_public_trade_is_canonical_and_derives_volume_once() -> None:
    trade = PublicTrade(
        exchange=" BinanceUSDM ",
        symbol="BTC/USDT:USDT",
        trade_id=" 123 ",
        timestamp=NOW.astimezone(timezone(timedelta(hours=5, minutes=30))),
        price=Decimal("100.5"),
        amount=Decimal(2),
        is_buyer_maker=False,
    )
    assert trade.exchange == "binanceusdm"
    assert trade.symbol == "BTCUSDT"
    assert trade.timestamp == NOW
    assert trade.quote_notional == Decimal("201.0")
    assert trade.taker_buy_volume == Decimal(2)
    assert trade.storage_row()["trade_id"] == "123"


def test_public_trade_rejects_invalid_identity_and_future_clock() -> None:
    with pytest.raises(ValueError, match="trade_id"):
        PublicTrade("binanceusdm", "BTCUSDT", "", NOW, 100, 1)
    trade = PublicTrade("binanceusdm", "BTCUSDT", "1", NOW + timedelta(seconds=6), 100, 1)
    with pytest.raises(ValueError, match="future slack"):
        trade.validate_clock(NOW, future_slack=timedelta(seconds=5))


def test_quote_update_rejects_non_executable_books_and_normalizes_utc() -> None:
    local = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
    quote = QuoteUpdate(ts=local, received_ts=local, bid=100.0, ask=100.1)
    assert quote.ts.tzinfo is UTC
    with pytest.raises(ValueError, match="below bid"):
        QuoteUpdate(ts=NOW, bid=101.0, ask=100.0)
    with pytest.raises(ValueError, match="positive"):
        QuoteUpdate(ts=NOW, bid=0.0, ask=100.0)


def test_lane_bbo_freezes_lane_consumed_parity_atom() -> None:
    row = LaneBBO(
        exchange="BINANCEUSDM",
        symbol="BTC/USDT:USDT",
        lane_id="squeeze-btc",
        bid=Decimal(100),
        ask=Decimal("100.1"),
        ts=NOW,
        received_ts=NOW + timedelta(milliseconds=2),
        sequence=9,
        source="bookTicker",
        overflow_drops=0,
        captured_at_ms=int(NOW.timestamp() * 1000),
    ).storage_row()
    assert row["symbol"] == "BTCUSDT"
    assert row["sequence"] == "9"
    assert row["bid"] == 100.0
