"""Live market data feeds.

Public data only — no credentials, no orders, no risk decisions. This module
produces exactly two things for the trading loop:

- a queue of CLOSED candles (a forming candle is emitted only when the next
  interval's first update proves it closed — the live equivalent of the
  backtester's bar-close discipline)
- a fresh MarketState (quote-derived spread, last-known funding via periodic
  REST refresh, and honest staleness: `last_update` is the wall-clock time
  of the last websocket event, so the gateway's data-freshness check fails
  naturally when the stream stalls)

Failure posture: errors mark the feed unhealthy and retry with bounded
backoff. An unhealthy or stale feed doesn't need to block anything itself —
the risk gateway already rejects on `exchange_healthy`/`data_freshness`,
which is where that decision belongs.

CCXT Pro is preferred for low-latency websocket venues. Some venues in the
architecture (notably Delta in current CCXT) expose public REST data but no
CCXT Pro websocket class; those use ``RestPollingMarketFeed`` so the lane can
still be observed in paper/shadow without pretending to be a fast path.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from vnedge.data.ccxt_client import create_ccxt_async_exchange
from vnedge.data.schemas import TIMEFRAME_MS
from vnedge.risk.risk_manager import MarketState

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_ERRORS = 5
_BACKOFF_SECONDS = 2.0
_DEFAULT_REST_CANDLE_POLL_SECONDS = 10.0
_DEFAULT_REST_QUOTE_POLL_SECONDS = 2.0
_VALIDATED_CCXT_PRO_FEEDS = {"binanceusdm", "bybit"}


class LiveMarketFeed:
    def __init__(
        self,
        exchange_id: str = "binanceusdm",
        *,
        symbol: str,
        timeframe: str = "1m",
        slippage_est_bps: float = 2.0,
        funding_refresh_seconds: float = 900.0,
    ) -> None:
        import ccxt.pro as ccxtpro  # heavy import kept local

        if not hasattr(ccxtpro, exchange_id):
            raise ValueError(f"unknown CCXT Pro exchange id: {exchange_id}")
        self._ex = getattr(ccxtpro, exchange_id)({"enableRateLimit": True})
        self.exchange_id = exchange_id
        self.feed_mode = "live ws"
        self.symbol = symbol
        self.timeframe = timeframe
        self.slippage_est_bps = slippage_est_bps
        self.funding_refresh_seconds = funding_refresh_seconds

        self.closed_candles: asyncio.Queue[list] = asyncio.Queue()
        self.quote: tuple[float, float] | None = None  # (bid, ask)
        self.funding_rate: float = 0.0
        # SETTLED funding prints [(ts_ms, rate), ...] refreshed with the rate.
        # Strategies validated on settled-print series (funding-MR) must read
        # THIS, not funding_rate: the predicted rate is a different series
        # than research used, and mixing them silently shifts percentiles.
        self.funding_events: list[tuple[int, float]] = []
        self.last_event_at: datetime | None = None
        self.healthy: bool = False
        self.candles_closed = 0
        self._consecutive_errors = 0
        self._forming: list | None = None
        self._tasks: list[asyncio.Task] = []

    # --- Lifecycle ----------------------------------------------------------------
    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._watch_candles(), name="feed-candles"),
            asyncio.create_task(self._watch_quotes(), name="feed-quotes"),
            asyncio.create_task(self._refresh_funding(), name="feed-funding"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._ex.close()

    # --- Health ---------------------------------------------------------------------
    def _mark_ok(self) -> None:
        self.last_event_at = datetime.now(UTC)
        self._consecutive_errors = 0
        self.healthy = True

    def _mark_error(self, where: str, exc: Exception) -> None:
        self._consecutive_errors += 1
        if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
            self.healthy = False
        logger.warning("live feed %s error (%d consecutive): %s",
                       where, self._consecutive_errors, exc)

    def staleness_seconds(self, now: datetime | None = None) -> float:
        if self.last_event_at is None:
            return float("inf")
        return ((now or datetime.now(UTC)) - self.last_event_at).total_seconds()

    def market_state(self) -> MarketState:
        if self.quote is not None:
            bid, ask = self.quote
            spread_bps = (ask - bid) / ((ask + bid) / 2.0) * 10_000.0
        else:
            spread_bps = float("inf")  # no quote yet -> gateway rejects on spread
        return MarketState(
            symbol=self.symbol,
            last_update=self.last_event_at or datetime(1970, 1, 1, tzinfo=UTC),
            spread_bps=spread_bps,
            estimated_slippage_bps=self.slippage_est_bps,
            funding_rate=self.funding_rate,
            exchange_healthy=self.healthy,
        )

    # --- Stream loops -----------------------------------------------------------------
    async def _watch_candles(self) -> None:
        while True:
            try:
                rows = await self._ex.watch_ohlcv(self.symbol, self.timeframe)
                self._mark_ok()
                for row in rows:
                    if self._forming is None:
                        self._forming = row
                    elif row[0] > self._forming[0]:
                        # a newer interval started: the forming candle is closed
                        await self.closed_candles.put(self._forming)
                        self.candles_closed += 1
                        self._forming = row
                    else:
                        self._forming = row  # same interval, updated values
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect with backoff
                self._mark_error("candles", exc)
                await asyncio.sleep(_BACKOFF_SECONDS)

    async def _watch_quotes(self) -> None:
        # Top-of-book via watch_order_book: some venues' ticker streams
        # (e.g. Binance USDT-M 24h ticker) carry no bid/ask at all.
        # limit=50 is the common depth both Binance and Bybit accept for swaps
        # (Bybit rejects 5: only {1,50,200,1000}). We only read level 0.
        while True:
            try:
                book = await self._ex.watch_order_book(self.symbol, limit=50)
                if book["bids"] and book["asks"]:
                    bid = float(book["bids"][0][0])
                    ask = float(book["asks"][0][0])
                    if 0 < bid <= ask:
                        self.quote = (bid, ask)
                        self._mark_ok()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_error("quotes", exc)
                await asyncio.sleep(_BACKOFF_SECONDS)

    async def _refresh_funding(self) -> None:
        while True:
            try:
                data = await self._ex.fetch_funding_rate(self.symbol)
                rate = data.get("fundingRate")
                if rate is not None:
                    self.funding_rate = float(rate)
                await _refresh_funding_events(self)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_error("funding", exc)
            await asyncio.sleep(self.funding_refresh_seconds)


async def _refresh_funding_events(feed) -> None:
    """Refresh a feed's SETTLED funding prints (``funding_events``).

    Best effort: venues without funding history simply keep an empty list
    (their strategies use live accumulation instead). Only recent prints are
    needed — the seed history covers the deep past; this keeps the tail fresh
    so the live series matches the research construction print-for-print.
    """
    ex = feed._ex
    if not ex.has.get("fetchFundingRateHistory"):
        return
    rows = await ex.fetch_funding_rate_history(feed.symbol, limit=10)
    events: list[tuple[int, float]] = []
    for row in rows or []:
        ts, rate = row.get("timestamp"), row.get("fundingRate")
        if ts is not None and rate is not None:
            events.append((int(ts), float(rate)))
    if events:
        feed.funding_events = sorted(events)


class RestPollingMarketFeed:
    """Public REST fallback feed for venues without CCXT Pro websocket support.

    This is an observability/shadow bridge, not a scalping feed. It polls
    top-of-book and OHLCV, emits only closed candles, and keeps the same
    surface as ``LiveMarketFeed`` so the runner remains single-path.
    """

    def __init__(
        self,
        exchange_id: str,
        *,
        symbol: str,
        timeframe: str = "1m",
        slippage_est_bps: float = 3.0,
        candle_poll_seconds: float = _DEFAULT_REST_CANDLE_POLL_SECONDS,
        quote_poll_seconds: float = _DEFAULT_REST_QUOTE_POLL_SECONDS,
        funding_refresh_seconds: float = 900.0,
    ) -> None:
        if timeframe not in TIMEFRAME_MS:
            raise ValueError(f"unsupported timeframe for REST polling feed: {timeframe}")
        self._ex = create_ccxt_async_exchange(exchange_id)
        self.exchange_id = exchange_id
        self.feed_mode = "rest polling"
        self.symbol = symbol
        self.timeframe = timeframe
        self.slippage_est_bps = slippage_est_bps
        self.candle_poll_seconds = candle_poll_seconds
        self.quote_poll_seconds = quote_poll_seconds
        self.funding_refresh_seconds = funding_refresh_seconds

        self.closed_candles: asyncio.Queue[list] = asyncio.Queue()
        self.quote: tuple[float, float] | None = None
        self.funding_rate: float = 0.0
        self.funding_events: list[tuple[int, float]] = []  # settled prints (ts_ms, rate)
        self.last_event_at: datetime | None = None
        self.healthy: bool = False
        self.candles_closed = 0
        self._consecutive_errors = 0
        self._last_emitted_candle_ts: int | None = None
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._poll_candles(), name="rest-feed-candles"),
            asyncio.create_task(self._poll_quotes(), name="rest-feed-quotes"),
            asyncio.create_task(self._refresh_funding(), name="rest-feed-funding"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._ex.close()

    def _mark_ok(self) -> None:
        self.last_event_at = datetime.now(UTC)
        self._consecutive_errors = 0
        self.healthy = True

    def _mark_error(self, where: str, exc: Exception) -> None:
        self._consecutive_errors += 1
        if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
            self.healthy = False
        logger.warning("REST feed %s %s error (%d consecutive): %s",
                       self.exchange_id, where, self._consecutive_errors, exc)

    def staleness_seconds(self, now: datetime | None = None) -> float:
        if self.last_event_at is None:
            return float("inf")
        return ((now or datetime.now(UTC)) - self.last_event_at).total_seconds()

    def market_state(self) -> MarketState:
        if self.quote is not None:
            bid, ask = self.quote
            spread_bps = (ask - bid) / ((ask + bid) / 2.0) * 10_000.0
        else:
            spread_bps = float("inf")
        return MarketState(
            symbol=self.symbol,
            last_update=self.last_event_at or datetime(1970, 1, 1, tzinfo=UTC),
            spread_bps=spread_bps,
            estimated_slippage_bps=self.slippage_est_bps,
            funding_rate=self.funding_rate,
            exchange_healthy=self.healthy,
        )

    async def _poll_candles(self) -> None:
        step_ms = TIMEFRAME_MS[self.timeframe]
        while True:
            try:
                now_ms = int(datetime.now(UTC).timestamp() * 1000)
                since = now_ms - 4 * step_ms
                rows = await self._ex.fetch_ohlcv(
                    self.symbol, self.timeframe, since=since, limit=4
                )
                closed = self._latest_closed_row(rows, now_ms, step_ms)
                if closed is not None and closed[0] != self._last_emitted_candle_ts:
                    await self.closed_candles.put(closed)
                    self._last_emitted_candle_ts = int(closed[0])
                    self.candles_closed += 1
                if rows:
                    self._mark_ok()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_error("candles", exc)
                await asyncio.sleep(_BACKOFF_SECONDS)
            await asyncio.sleep(self.candle_poll_seconds)

    @staticmethod
    def _latest_closed_row(rows: list[list], now_ms: int, step_ms: int) -> list | None:
        closed = [row for row in rows if int(row[0]) + step_ms <= now_ms]
        if not closed:
            return None
        return closed[-1]

    async def _poll_quotes(self) -> None:
        while True:
            try:
                book = await self._ex.fetch_order_book(self.symbol, limit=5)
                if book.get("bids") and book.get("asks"):
                    bid = float(book["bids"][0][0])
                    ask = float(book["asks"][0][0])
                    if 0 < bid <= ask:
                        self.quote = (bid, ask)
                        self._mark_ok()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_error("quotes", exc)
                await asyncio.sleep(_BACKOFF_SECONDS)
            await asyncio.sleep(self.quote_poll_seconds)

    async def _refresh_funding(self) -> None:
        while True:
            try:
                if self._ex.has.get("fetchFundingRate"):
                    data = await self._ex.fetch_funding_rate(self.symbol)
                    rate = data.get("fundingRate")
                    if rate is not None:
                        self.funding_rate = float(rate)
                await _refresh_funding_events(self)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_error("funding", exc)
            await asyncio.sleep(self.funding_refresh_seconds)


class DeltaWsFeed(RestPollingMarketFeed):
    """Delta India feed: REST closed candles + NATIVE websocket quotes/funding.

    Delta has no CCXT Pro class, so candles still come from REST (closed 1h
    bars, same discipline as the polling feed). But quotes and funding no
    longer poll every couple of seconds — they are pushed from Delta's native
    public websocket (``DeltaPublicWsClient``): top-of-book from ``l2_orderbook``
    and funding from the ``funding_rate`` channel. Staleness mirrors the last
    websocket event, so the gateway's freshness check reflects the real stream.
    """

    def __init__(
        self,
        exchange_id: str,
        *,
        symbol: str,
        timeframe: str = "1m",
        slippage_est_bps: float = 3.0,
        candle_poll_seconds: float = _DEFAULT_REST_CANDLE_POLL_SECONDS,
    ) -> None:
        super().__init__(
            exchange_id,
            symbol=symbol,
            timeframe=timeframe,
            slippage_est_bps=slippage_est_bps,
            candle_poll_seconds=candle_poll_seconds,
        )
        from vnedge.exchange.delta_ws import DeltaPublicWsClient, delta_native_symbol

        self.feed_mode = "delta native ws + rest candles"
        self._native_symbol = delta_native_symbol(symbol)
        self._ws = DeltaPublicWsClient([self._native_symbol])

    async def start(self) -> None:
        await self._ws.start()
        self._tasks = [
            asyncio.create_task(self._poll_candles(), name="delta-feed-candles"),
            asyncio.create_task(self._sync_ws_state(), name="delta-feed-ws-sync"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._ws.stop()
        await self._ex.close()

    async def _sync_ws_state(self) -> None:
        """Mirror native websocket state into the polling-feed surface."""
        while True:
            try:
                quote = self._ws.quote(self._native_symbol)
                if quote is not None:
                    self.quote = quote
                fr = self._ws.funding_rate.get(self._native_symbol)
                if fr is not None:
                    self.funding_rate = fr
                # honest staleness/health: track the real stream, not this loop
                if self._ws.last_event_at is not None:
                    self.last_event_at = self._ws.last_event_at
                    self.healthy = self._ws.healthy
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_error("ws-sync", exc)
            await asyncio.sleep(0.5)


def supports_ccxt_pro_feed(exchange_id: str) -> bool:
    """Whether this CCXT id has the websocket methods our live feed needs."""
    try:
        import ccxt.pro as ccxtpro  # heavy import kept local
    except Exception:  # pragma: no cover - import failure is environment-specific
        return False
    return exchange_id in _VALIDATED_CCXT_PRO_FEEDS and hasattr(ccxtpro, exchange_id)


_DELTA_NATIVE_WS_IDS = {"delta_india", "delta", "deltaindia"}


def create_market_feed(
    exchange_id: str,
    *,
    symbol: str,
    timeframe: str = "1m",
) -> LiveMarketFeed | RestPollingMarketFeed:
    if supports_ccxt_pro_feed(exchange_id):
        return LiveMarketFeed(exchange_id, symbol=symbol, timeframe=timeframe)
    if exchange_id in _DELTA_NATIVE_WS_IDS:
        # Delta has no CCXT Pro class but does have a native public websocket.
        return DeltaWsFeed(exchange_id, symbol=symbol, timeframe=timeframe)
    return RestPollingMarketFeed(exchange_id, symbol=symbol, timeframe=timeframe)
