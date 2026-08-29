"""Shared public feeds: one candle stream per timeframe, one BBO per market."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import cast

from vnedge.exchange.live_feed import (
    QUOTE_ACCEPTANCE_BUFFER_SIZE,
    LiveMarketFeed,
    QuoteUpdate,
    RestPollingMarketFeed,
    create_market_feed,
    quote_overflow_drops,
    record_quote_overflow,
)
from vnedge.risk.risk_manager import MarketState

FeedKey = tuple[str, str, str]
QuoteKey = tuple[str, str]
_Feed = LiveMarketFeed | RestPollingMarketFeed
FeedFactory = Callable[..., _Feed]


class _FeedEntry:
    def __init__(self, key: FeedKey | QuoteKey, feed: _Feed) -> None:
        self.key = key
        self.feed = feed
        self.views: list[SharedFeedView] = []
        self.fanout_task: asyncio.Task | None = None
        self.started = False
        self.start_lock = asyncio.Lock()


class SharedFeedView:
    """Lane-local queues backed by separate shared candle and quote owners."""

    def __init__(self, registry: SharedFeedRegistry, candle: _FeedEntry, quote: _FeedEntry) -> None:
        self._registry = registry
        self._candle_entry = candle
        self._quote_entry = quote
        self.closed_candles: asyncio.Queue[list] = asyncio.Queue()
        self.quote_updates: asyncio.Queue[QuoteUpdate] = asyncio.Queue(
            maxsize=QUOTE_ACCEPTANCE_BUFFER_SIZE
        )
        self.candles_closed = 0
        self._stopped = False

    async def start(self) -> None:
        if self._stopped:
            raise RuntimeError("cannot start a released SharedFeedView")
        await self._registry._start_entry(self._candle_entry, quote=False)
        await self._registry._start_entry(self._quote_entry, quote=True)

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        await self._registry._release(self)

    def _deliver(self, row: list) -> None:
        self.closed_candles.put_nowait(row)
        self.candles_closed += 1

    def _deliver_quote(self, quote: QuoteUpdate) -> None:
        if self.quote_updates.full():
            try:
                self.quote_updates.get_nowait()
                record_quote_overflow(self.quote_updates)
            except asyncio.QueueEmpty:  # pragma: no cover
                pass
        self.quote_updates.put_nowait(quote)

    @property
    def _feed(self) -> _Feed:
        return self._candle_entry.feed

    @property
    def _quote_feed(self) -> _Feed:
        return self._quote_entry.feed

    @property
    def quote_overflow_drops(self) -> int:
        return quote_overflow_drops(self._quote_feed.quote_updates) + quote_overflow_drops(
            self.quote_updates
        )

    @property
    def exchange_id(self) -> str:
        return self._feed.exchange_id

    @property
    def symbol(self) -> str:
        return self._feed.symbol

    @property
    def timeframe(self) -> str:
        return self._feed.timeframe

    @property
    def feed_mode(self) -> str:
        count = len(self._quote_entry.views)
        return self._feed.feed_mode if count <= 1 else f"{self._feed.feed_mode}, BBO shared x{count}"

    @property
    def slippage_est_bps(self) -> float:
        return self._quote_feed.slippage_est_bps

    @property
    def quote(self) -> tuple[float, float] | None:
        return self._quote_feed.quote

    @property
    def forming_candle(self) -> list | None:
        return getattr(self._feed, "forming_candle", None)

    @property
    def funding_rate(self) -> float:
        return self._quote_feed.funding_rate

    @property
    def funding_events(self) -> list[tuple[int, float]]:
        return self._quote_feed.funding_events

    @property
    def book_metrics(self) -> dict | None:
        return self._quote_feed.book_metrics

    @property
    def healthy(self) -> bool:
        return self._feed.healthy and self._quote_feed.healthy

    @property
    def last_event_at(self) -> datetime | None:
        values = [x for x in (self._feed.last_event_at, self._quote_feed.last_event_at) if x]
        return min(values) if values else None

    def staleness_seconds(self, now: datetime | None = None) -> float:
        return max(self._feed.staleness_seconds(now), self._quote_feed.staleness_seconds(now))

    def market_state(self) -> MarketState:
        state = self._quote_feed.market_state()
        if self._feed.healthy:
            return state
        return MarketState(
            symbol=state.symbol,
            last_update=state.last_update,
            spread_bps=state.spread_bps,
            estimated_slippage_bps=state.estimated_slippage_bps,
            funding_rate=state.funding_rate,
            exchange_healthy=False,
            data_degraded=True,
            data_quality="degraded",
            data_quality_reason="timeframe candle transport unhealthy",
        )


class SharedFeedRegistry:
    """Refcounted candle feeds plus a market-wide BBO/funding feed."""

    def __init__(self, feed_factory: FeedFactory | None = None) -> None:
        self._factory = feed_factory or create_market_feed
        self._entries: dict[FeedKey, _FeedEntry] = {}
        self._quote_entries: dict[QuoteKey, _FeedEntry] = {}

    def acquire(self, exchange_id: str, *, symbol: str, timeframe: str = "1m") -> SharedFeedView:
        candle_key: FeedKey = (exchange_id, symbol, timeframe)
        candle = self._entries.get(candle_key)
        if candle is None:
            candle = _FeedEntry(
                candle_key,
                self._factory(
                    exchange_id, symbol=symbol, timeframe=timeframe,
                    enable_candles=True, enable_quotes=False, enable_funding=False,
                ),
            )
            self._entries[candle_key] = candle
        quote_key: QuoteKey = (exchange_id, symbol)
        quote = self._quote_entries.get(quote_key)
        if quote is None:
            quote = _FeedEntry(
                quote_key,
                self._factory(
                    exchange_id, symbol=symbol, timeframe="1m",
                    enable_candles=False, enable_quotes=True, enable_funding=True,
                ),
            )
            self._quote_entries[quote_key] = quote
        view = SharedFeedView(self, candle, quote)
        candle.views.append(view)
        quote.views.append(view)
        return view

    def active_feeds(self) -> dict[FeedKey, int]:
        return {key: len(entry.views) for key, entry in self._entries.items()}

    def active_quote_feeds(self) -> dict[QuoteKey, int]:
        return {key: len(entry.views) for key, entry in self._quote_entries.items()}

    async def _start_entry(self, entry: _FeedEntry, *, quote: bool) -> None:
        async with entry.start_lock:
            if entry.started:
                return
            await entry.feed.start()
            target = self._fan_out_quotes(entry) if quote else self._fan_out(entry)
            entry.fanout_task = asyncio.create_task(
                target, name=f"{'quote' if quote else 'candle'}-fanout-{'-'.join(entry.key)}"
            )
            entry.started = True

    async def _fan_out(self, entry: _FeedEntry) -> None:
        while True:
            row = await entry.feed.closed_candles.get()
            for view in list(entry.views):
                view._deliver(row)

    async def _fan_out_quotes(self, entry: _FeedEntry) -> None:
        while True:
            quote = await entry.feed.quote_updates.get()
            for view in list(entry.views):
                view._deliver_quote(quote)

    async def _stop_empty(self, entry: _FeedEntry) -> None:
        if entry.views:
            return
        if entry.fanout_task is not None:
            entry.fanout_task.cancel()
            await asyncio.gather(entry.fanout_task, return_exceptions=True)
            entry.fanout_task = None
        entry.started = False
        await entry.feed.stop()

    async def _release(self, view: SharedFeedView) -> None:
        candle, quote = view._candle_entry, view._quote_entry
        if view in candle.views:
            candle.views.remove(view)
        if view in quote.views:
            quote.views.remove(view)
        if not candle.views:
            self._entries.pop(cast(FeedKey, candle.key), None)
            await self._stop_empty(candle)
        if not quote.views:
            self._quote_entries.pop(cast(QuoteKey, quote.key), None)
            await self._stop_empty(quote)


_DEFAULT_REGISTRY = SharedFeedRegistry()


def shared_feed_registry() -> SharedFeedRegistry:
    return _DEFAULT_REGISTRY


def acquire_market_feed(
    exchange_id: str, *, symbol: str, timeframe: str = "1m"
) -> SharedFeedView:
    """Acquire from the process-wide registry used by runtime lane builders."""
    return _DEFAULT_REGISTRY.acquire(exchange_id, symbol=symbol, timeframe=timeframe)
