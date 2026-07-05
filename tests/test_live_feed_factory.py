"""Market feed factory — websocket where possible, REST fallback where needed."""

from vnedge.data.ccxt_client import create_ccxt_async_exchange, resolve_ccxt_exchange_id
from vnedge.exchange.live_feed import (
    LiveMarketFeed,
    RestPollingMarketFeed,
    create_market_feed,
    supports_ccxt_pro_feed,
)


async def test_delta_india_uses_india_rest_host():
    assert resolve_ccxt_exchange_id("delta_india") == "delta"
    exchange = create_ccxt_async_exchange("delta_india")
    try:
        assert exchange.urls["api"]["public"] == "https://api.india.delta.exchange"
        assert exchange.urls["api"]["private"] == "https://api.india.delta.exchange"
    finally:
        await exchange.close()


async def test_feed_factory_routes_validated_websocket_venues():
    assert supports_ccxt_pro_feed("binanceusdm") is True
    assert supports_ccxt_pro_feed("bybit") is True

    binance = create_market_feed("binanceusdm", symbol="BTC/USDT:USDT")
    bybit = create_market_feed("bybit", symbol="BTC/USDT:USDT")

    try:
        assert isinstance(binance, LiveMarketFeed)
        assert isinstance(bybit, LiveMarketFeed)
    finally:
        await binance.stop()
        await bybit.stop()


async def test_feed_factory_routes_delta_to_rest_polling():
    assert supports_ccxt_pro_feed("delta_india") is False

    feed = create_market_feed("delta_india", symbol="BTC/USD:USD")
    try:
        assert isinstance(feed, RestPollingMarketFeed)
        assert feed.exchange_id == "delta_india"
    finally:
        await feed.stop()


def test_rest_polling_latest_closed_row_uses_only_closed_candles():
    now_ms = 10_000
    step_ms = 1_000
    rows = [
        [7_000, 1.0, 1.0, 1.0, 1.0, 1.0],
        [8_000, 2.0, 2.0, 2.0, 2.0, 2.0],
        [9_500, 3.0, 3.0, 3.0, 3.0, 3.0],
    ]

    assert RestPollingMarketFeed._latest_closed_row(rows, now_ms, step_ms) == rows[1]
