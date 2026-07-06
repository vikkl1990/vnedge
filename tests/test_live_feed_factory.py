"""Market feed factory — websocket where possible, REST fallback where needed."""

from vnedge.data.ccxt_client import create_ccxt_async_exchange, resolve_ccxt_exchange_id
from vnedge.exchange.live_feed import (
    DeltaWsFeed,
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


async def test_feed_factory_routes_delta_to_native_websocket():
    # Delta has no CCXT Pro class, but it does have a native public websocket.
    assert supports_ccxt_pro_feed("delta_india") is False

    feed = create_market_feed("delta_india", symbol="BTC/USD:USD")
    try:
        assert isinstance(feed, DeltaWsFeed)
        # DeltaWsFeed keeps the RestPollingMarketFeed surface (candles via REST).
        assert isinstance(feed, RestPollingMarketFeed)
        assert feed.exchange_id == "delta_india"
        assert feed._native_symbol == "BTCUSD"
        assert "native ws" in feed.feed_mode
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


class _HistExchange:
    """Fake ccxt exchange: settled funding history support."""
    has = {"fetchFundingRateHistory": True}

    async def fetch_funding_rate_history(self, symbol, limit=10):
        return [
            {"timestamp": 2_000, "fundingRate": "0.0002"},
            {"timestamp": 1_000, "fundingRate": "0.0001"},
            {"timestamp": None, "fundingRate": "0.9"},      # dropped
            {"timestamp": 3_000, "fundingRate": None},       # dropped
        ]


class _NoHistExchange:
    has = {"fetchFundingRateHistory": False}


async def test_refresh_funding_events_populates_sorted_settled_prints():
    from vnedge.exchange.live_feed import _refresh_funding_events

    class Feed:
        _ex = _HistExchange()
        symbol = "BTC/USDT:USDT"
        funding_events: list = []

    feed = Feed()
    await _refresh_funding_events(feed)
    assert feed.funding_events == [(1_000, 0.0001), (2_000, 0.0002)]


async def test_refresh_funding_events_noop_without_venue_support():
    from vnedge.exchange.live_feed import _refresh_funding_events

    class Feed:
        _ex = _NoHistExchange()
        symbol = "BTC/USD:USD"
        funding_events: list = []

    feed = Feed()
    await _refresh_funding_events(feed)
    assert feed.funding_events == []
