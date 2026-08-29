"""Shared feed registry — fan-out, refcounted lifecycle, view isolation.

No network: a fake feed with the LiveMarketFeed surface stands in for the
real thing, so these tests pin the multiplexing contract: one real feed per
(exchange, symbol, timeframe), every closed candle delivered to EVERY view,
and the last view release stopping the real feed.
"""

import asyncio
from datetime import UTC, datetime

import pytest

from vnedge.exchange.feed_registry import SharedFeedRegistry
from vnedge.exchange.live_feed import (
    QUOTE_ACCEPTANCE_BUFFER_SIZE,
    QuoteUpdate,
    _publish_latest_quote,
    quote_overflow_drops,
)
from vnedge.risk.risk_manager import MarketState


class FakeFeed:
    """Stands in for LiveMarketFeed/RestPollingMarketFeed in the registry."""

    def __init__(
        self,
        exchange_id: str,
        *,
        symbol: str,
        timeframe: str = "1m",
        **streams,
    ) -> None:
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.timeframe = timeframe
        self.feed_mode = "fake ws"
        self.slippage_est_bps = 2.0
        self.closed_candles: asyncio.Queue[list] = asyncio.Queue()
        self.quote_updates: asyncio.Queue[QuoteUpdate] = asyncio.Queue(maxsize=1)
        self.quote: tuple[float, float] | None = (100.0, 101.0)
        self.funding_rate = 0.0001
        self.funding_events = [(1_000, 0.0001)]
        self.book_metrics = {"spread_bps": 1.0}
        self.healthy = True
        self.last_event_at = datetime.now(UTC)
        self.candles_closed = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.streams = streams

    async def start(self) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1

    def staleness_seconds(self, now=None) -> float:
        return 1.5

    def market_state(self) -> MarketState:
        return MarketState(
            symbol=self.symbol,
            last_update=self.last_event_at,
            spread_bps=1.0,
            estimated_slippage_bps=self.slippage_est_bps,
            funding_rate=self.funding_rate,
            exchange_healthy=self.healthy,
        )


def make_registry():
    created: list[FakeFeed] = []

    def factory(exchange_id, *, symbol, timeframe="1m", **streams):
        feed = FakeFeed(exchange_id, symbol=symbol, timeframe=timeframe, **streams)
        created.append(feed)
        return feed

    return SharedFeedRegistry(feed_factory=factory), created


async def drain(view, n, timeout=1.0):
    return [
        await asyncio.wait_for(view.closed_candles.get(), timeout=timeout)
        for _ in range(n)
    ]


async def test_fan_out_delivers_every_candle_to_all_views():
    registry, created = make_registry()
    a = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h")
    b = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h")

    # same key -> ONE real feed, created once, started once
    assert len(created) == 2  # one TF candle source + one market-wide BBO source
    await a.start()
    await b.start()
    assert created[0].start_calls == created[1].start_calls == 1

    candles = [[1_000, 1, 2, 0.5, 1.5, 10], [2_000, 1.5, 3, 1, 2, 11], [3_000, 2, 4, 2, 3, 12]]
    for row in candles:
        created[0].closed_candles.put_nowait(row)

    # BOTH views receive EVERY candle, in order — no competition on one queue
    assert await drain(a, 3) == candles
    assert await drain(b, 3) == candles
    assert a.candles_closed == 3
    assert b.candles_closed == 3

    await a.stop()
    await b.stop()


async def test_quote_fanout_delivers_bounded_history_to_every_view() -> None:
    registry, created = make_registry()
    a = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="5m")
    b = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="5m")
    await a.start()
    await b.start()

    quote = QuoteUpdate(ts=datetime.now(UTC), bid=100.0, ask=100.1)
    created[1].quote_updates.put_nowait(quote)
    assert await asyncio.wait_for(a.quote_updates.get(), timeout=1.0) == quote
    assert await asyncio.wait_for(b.quote_updates.get(), timeout=1.0) == quote

    # Per-view queues retain ordered acceptance evidence instead of silently
    # replacing every observation with the latest quote.
    older = QuoteUpdate(ts=datetime.now(UTC), bid=101.0, ask=101.1)
    a._deliver_quote(older)
    latest = QuoteUpdate(ts=datetime.now(UTC), bid=102.0, ask=102.1)
    a._deliver_quote(latest)
    assert a.quote_updates.qsize() == 2
    assert a.quote_updates.get_nowait() == older
    assert a.quote_updates.get_nowait() == latest
    await a.stop()
    await b.stop()


async def test_different_timeframes_share_one_market_bbo() -> None:
    registry, created = make_registry()
    five = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="5m")
    hour = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h")
    await five.start()
    await hour.start()

    assert len(created) == 3  # 5m candles, shared BBO, 1h candles
    assert registry.active_quote_feeds() == {("binanceusdm", "BTC/USDT:USDT"): 2}
    quote = QuoteUpdate(ts=datetime.now(UTC), bid=100.0, ask=100.1)
    created[1].quote_updates.put_nowait(quote)
    assert await asyncio.wait_for(five.quote_updates.get(), timeout=1.0) == quote
    assert await asyncio.wait_for(hour.quote_updates.get(), timeout=1.0) == quote
    assert created[1].streams == {
        "enable_candles": False,
        "enable_quotes": True,
        "enable_funding": True,
    }
    await five.stop()
    await hour.stop()


def test_quote_update_preserves_event_and_receive_clocks() -> None:
    event_ts = datetime(2026, 8, 20, tzinfo=UTC)
    received_ts = event_ts.replace(microsecond=500_000)
    quote = QuoteUpdate(
        ts=event_ts,
        bid=100.0,
        ask=100.1,
        received_ts=received_ts,
        sequence=77,
        source="binanceusdm:watch_order_book",
        exchange_timestamped=True,
    )
    assert quote.ingest_lag_seconds == 0.5
    assert quote.sequence == 77
    assert quote.exchange_timestamped is True


def test_quote_update_rejects_naive_clocks() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        QuoteUpdate(
            ts=datetime(2026, 8, 20),  # noqa: DTZ001 — deliberately invalid input
            bid=100.0,
            ask=100.1,
        )


def test_quote_publisher_marks_exchange_time_and_replaces_stale_item() -> None:
    queue: asyncio.Queue[QuoteUpdate] = asyncio.Queue(maxsize=1)
    event_ts = datetime(2026, 8, 20, tzinfo=UTC)
    received_ts = event_ts.replace(microsecond=250_000)
    _publish_latest_quote(queue, bid=99.9, ask=100.0)
    published = _publish_latest_quote(
        queue,
        bid=100.0,
        ask=100.1,
        event_ts=event_ts,
        received_ts=received_ts,
        sequence=5,
        source="test:book",
    )
    assert queue.qsize() == 1
    assert queue.get_nowait() == published
    assert published.ts == event_ts
    assert published.received_ts == received_ts
    assert published.ingest_lag_seconds == 0.25
    assert published.exchange_timestamped is True
    assert quote_overflow_drops(queue) == 1


def test_per_view_quote_overflow_is_explicit_and_evicts_oldest() -> None:
    registry, _ = make_registry()
    view = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="5m")
    base = datetime(2026, 8, 20, tzinfo=UTC)

    for index in range(QUOTE_ACCEPTANCE_BUFFER_SIZE + 1):
        view._deliver_quote(
            QuoteUpdate(
                ts=base.replace(microsecond=index),
                bid=100.0 + index / 10_000,
                ask=100.1 + index / 10_000,
            )
        )

    assert view.quote_updates.qsize() == QUOTE_ACCEPTANCE_BUFFER_SIZE
    assert view.quote_overflow_drops == 1
    assert view.quote_updates.get_nowait().ts.microsecond == 1


async def test_refcounted_stop_only_last_release_stops_the_feed():
    registry, created = make_registry()
    a = registry.acquire("bybit", symbol="BTC/USDT:USDT", timeframe="1h")
    b = registry.acquire("bybit", symbol="BTC/USDT:USDT", timeframe="1h")
    await a.start()
    await b.start()

    await a.stop()
    assert created[0].stop_calls == created[1].stop_calls == 0
    assert registry.active_feeds() == {("bybit", "BTC/USDT:USDT", "1h"): 1}

    await b.stop()
    assert created[0].stop_calls == created[1].stop_calls == 1
    assert registry.active_feeds() == {}

    # next acquire builds a FRESH feed, not the stopped one
    c = registry.acquire("bybit", symbol="BTC/USDT:USDT", timeframe="1h")
    assert len(created) == 4
    await c.stop()
    assert created[2].stop_calls == created[3].stop_calls == 1


async def test_stopped_view_is_isolated_from_the_shared_stream():
    registry, created = make_registry()
    a = registry.acquire("binanceusdm", symbol="ETH/USDT:USDT", timeframe="1h")
    b = registry.acquire("binanceusdm", symbol="ETH/USDT:USDT", timeframe="1h")
    await a.start()
    await b.start()

    await a.stop()
    created[0].closed_candles.put_nowait([1_000, 1, 2, 0.5, 1.5, 10])

    assert await drain(b, 1) == [[1_000, 1, 2, 0.5, 1.5, 10]]
    assert a.closed_candles.empty()  # released view no longer receives
    assert a.candles_closed == 0

    # stop is idempotent and never double-releases the refcount
    await a.stop()
    assert created[0].stop_calls == created[1].stop_calls == 0
    with pytest.raises(RuntimeError, match="released"):
        await a.start()

    await b.stop()
    assert created[0].stop_calls == created[1].stop_calls == 1


async def test_views_proxy_shared_state_and_report_sharing():
    registry, created = make_registry()
    a = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h")
    assert a.feed_mode == "fake ws"  # single view: plain mode
    b = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h")

    feed = created[0]
    quote_feed = created[1]
    assert a.exchange_id == "binanceusdm"
    assert a.symbol == "BTC/USDT:USDT"
    assert a.timeframe == "1h"
    assert a.quote == quote_feed.quote
    assert a.funding_rate == quote_feed.funding_rate
    assert a.funding_events == quote_feed.funding_events
    assert a.book_metrics == quote_feed.book_metrics
    assert a.healthy is True
    assert a.last_event_at == feed.last_event_at
    assert a.staleness_seconds() == 1.5
    assert a.market_state() == quote_feed.market_state()
    assert b.feed_mode == "fake ws, BBO shared x2"

    # shared state mutates in ONE place and every view sees it
    quote_feed.quote = (200.0, 201.0)
    assert a.quote == b.quote == (200.0, 201.0)

    await a.stop()
    await b.stop()


async def test_different_keys_build_independent_feeds():
    registry, created = make_registry()
    a = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h")
    b = registry.acquire("binanceusdm", symbol="ETH/USDT:USDT", timeframe="1h")
    c = registry.acquire("binanceusdm", symbol="BTC/USDT:USDT", timeframe="1m")
    d = registry.acquire("bybit", symbol="BTC/USDT:USDT", timeframe="1h")

    assert len(created) == 7  # four TF feeds plus three market-wide BBO feeds
    assert len(registry.active_feeds()) == 4
    assert len(registry.active_quote_feeds()) == 3
    for view in (a, b, c, d):
        await view.stop()
    assert registry.active_feeds() == {}
    assert all(feed.stop_calls == 1 for feed in created)


async def test_build_lane_uses_the_shared_registry(monkeypatch, tmp_path):
    """multi_lane.build_lane must acquire feeds through the registry."""
    import vnedge.runtime.multi_lane as ml

    seen = []

    def fake_acquire(exchange_id, *, symbol, timeframe="1m"):
        seen.append((exchange_id, symbol, timeframe))
        raise RuntimeError("stop here — wiring verified")

    monkeypatch.setattr(ml, "acquire_market_feed", fake_acquire)

    # skip the network warmup by faking the REST client context
    class FakeRest:
        def __init__(self, exchange):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def fetch_candles(self, *a, **k):
            until = int(a[3])
            step = 3_600_000
            return [
                [until - 2 * step, 100, 101, 99, 100, 1],
                [until - step, 100, 101, 99, 100, 1],
            ]

        async def fetch_funding_history(self, *a, **k):
            return []

    monkeypatch.setattr(ml, "CcxtPublicClient", FakeRest)

    spec = ml.LaneSpec(lane_id="x", exchange="bybit", symbol="BTC/USDT:USDT",
                       timeframe="1h")
    provider = ml.MultiLaneProvider("x")

    with pytest.raises(RuntimeError, match="wiring verified"):
        await ml.build_lane(spec, provider, journal_dir=tmp_path)
    assert seen == [("bybit", "BTC/USDT:USDT", "1h")]
