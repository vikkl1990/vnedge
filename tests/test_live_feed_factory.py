"""Market feed factory — websocket where possible, REST fallback where needed."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from vnedge.data.ccxt_client import create_ccxt_async_exchange, resolve_ccxt_exchange_id
from vnedge.exchange import live_feed as live_feed_module
from vnedge.exchange.live_feed import (
    DeltaWsFeed,
    LiveMarketFeed,
    RestPollingMarketFeed,
    _advance_forming,
    create_market_feed,
    supports_ccxt_pro_feed,
)


def test_forming_updates_are_monotonic_across_reconnect_replays() -> None:
    current = [2_000, 10.0, 12.0, 9.0, 11.0, 5.0]
    stale = [1_000, 8.0, 20.0, 1.0, 19.0, 99.0]
    same = [2_000, 10.0, 13.0, 9.0, 12.0, 7.0]
    newer = [3_000, 12.0, 14.0, 11.0, 13.0, 2.0]

    forming, closed = _advance_forming(current, stale)
    assert forming is current and closed is None
    forming, closed = _advance_forming(forming, same)
    assert forming is same and closed is None
    forming, closed = _advance_forming(forming, newer)
    assert forming is newer and closed is same


def test_feed_factory_rejects_an_unknown_exchange_instead_of_falling_back() -> None:
    with pytest.raises(ValueError, match="unknown CCXT exchange id"):
        create_market_feed("binance_typo", symbol="BTC/USDT:USDT")


async def test_live_feed_starts_one_order_book_consumer() -> None:
    feed = object.__new__(LiveMarketFeed)
    blocker = asyncio.Event()

    async def wait_forever() -> None:
        await blocker.wait()

    feed._watch_candles = wait_forever
    feed._refresh_funding = wait_forever
    feed._watch_book = wait_forever
    feed._tasks = []

    await feed.start()
    try:
        assert [task.get_name() for task in feed._tasks] == [
            "feed-candles",
            "feed-funding",
            "feed-book",
        ]
    finally:
        for task in feed._tasks:
            task.cancel()
        await asyncio.gather(*feed._tasks, return_exceptions=True)


@pytest.mark.network
async def test_delta_india_uses_india_rest_host():
    assert resolve_ccxt_exchange_id("delta_india") == "delta"
    exchange = create_ccxt_async_exchange("delta_india")
    try:
        assert exchange.urls["api"]["public"] == "https://api.india.delta.exchange"
        assert exchange.urls["api"]["private"] == "https://api.india.delta.exchange"
    finally:
        await exchange.close()


@pytest.mark.network
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


@pytest.mark.network
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


@pytest.mark.network
async def test_delta_ws_candles_emit_closed_bars_with_monotonic_dedup():
    feed = create_market_feed("delta_india", symbol="BTC/USD:USD", timeframe="1h")
    try:
        assert isinstance(feed, DeltaWsFeed)
        # the native ws client subscribes the candle channel for OUR timeframe
        assert "candlestick_1h" in feed._ws.channels
        assert feed._ws_candles_fresh() is False  # nothing from ws yet

        row = [1_000, 1.0, 2.0, 0.5, 1.5, 10.0]
        feed._on_ws_candle("BTCUSD", "1h", row)
        assert feed.closed_candles.get_nowait() == row
        assert feed.candles_closed == 1
        assert feed._ws_candles_fresh() is True

        # same bar again (e.g. the REST fallback catching up) is deduplicated
        assert feed._emit_closed(row) is False
        # other symbols/timeframes never leak into this feed's queue
        feed._on_ws_candle("ETHUSD", "1h", [3_601_000, 1, 1, 1, 1, 1])
        feed._on_ws_candle("BTCUSD", "1m", [3_601_000, 1, 1, 1, 1, 1])
        assert feed.closed_candles.empty()

        # the next interval's close flows through
        nxt = [3_601_000, 1.5, 2.5, 1.0, 2.0, 11.0]
        feed._on_ws_candle("BTCUSD", "1h", nxt)
        assert feed.closed_candles.get_nowait() == nxt
        assert feed.candles_closed == 2
    finally:
        await feed.stop()


class _FakeRestExchange:
    has: dict = {}

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=4):
        self.calls += 1
        return self.rows

    async def close(self):
        pass


@pytest.mark.network
async def test_delta_rest_candle_fallback_only_when_ws_is_stale(monkeypatch):
    # Keep the fallback's closure boundary fixed.  This test intentionally
    # runs a polling loop for several wall-clock sleeps; without a frozen
    # exchange clock it becomes nondeterministic when the suite crosses a
    # real UTC minute boundary.
    fixed_now = datetime.now(UTC)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(live_feed_module, "datetime", _FrozenDateTime)
    feed = create_market_feed("delta_india", symbol="BTC/USD:USD", timeframe="1m")
    step_ms = 60_000
    now_ms = int(fixed_now.timestamp() * 1000)
    closed_ts = (now_ms // step_ms - 1) * step_ms  # latest fully closed minute
    fake = _FakeRestExchange([
        [closed_ts, 1.0, 2.0, 0.5, 1.5, 10.0],
        [closed_ts + step_ms, 1.5, 1.6, 1.4, 1.5, 2.0],  # forming, never emitted
    ])
    await feed._ex.close()
    feed._ex = fake
    feed.candle_poll_seconds = 0.01
    try:
        # ws candles fresh -> the fallback loop never touches REST
        feed._last_ws_candle_at = datetime.now(UTC)
        task = asyncio.create_task(feed._poll_candles())
        await asyncio.sleep(0.08)
        assert fake.calls == 0
        assert feed.closed_candles.empty()

        # no ws candle for 2x the timeframe -> REST emits the closed bar
        feed._last_ws_candle_at = datetime.now(UTC) - timedelta(seconds=121)
        for _ in range(100):
            if fake.calls:
                break
            await asyncio.sleep(0.01)
        assert fake.calls >= 1
        emitted = await asyncio.wait_for(feed.closed_candles.get(), timeout=1.0)
        assert emitted[0] == closed_ts
        assert feed.forming_candle is not None
        assert feed.forming_candle[0] == closed_ts + step_ms

        # repeat polls do not re-emit the same bar (monotonic guard)
        calls_before = fake.calls
        for _ in range(100):
            if fake.calls > calls_before:
                break
            await asyncio.sleep(0.01)
        assert feed.closed_candles.empty()

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
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


def test_feed_market_state_fails_closed_on_application_data_silence():
    feed = RestPollingMarketFeed.__new__(RestPollingMarketFeed)
    feed.symbol = "BTC/USDT:USDT"
    feed.quote = (100.0, 100.1)
    feed.slippage_est_bps = 2.0
    feed.funding_rate = 0.0
    feed.healthy = True
    feed.data_silence_seconds = 60.0
    feed.last_event_at = datetime.now(UTC) - timedelta(seconds=61)

    stale = feed.market_state()
    assert stale.data_degraded is True
    assert stale.data_quality == "stale"
    assert "silent" in stale.data_quality_reason

    feed.last_event_at = datetime.now(UTC)
    healthy = feed.market_state()
    assert healthy.data_degraded is False
    assert healthy.data_quality == "ok"


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
