"""Live market data feed via CCXT Pro websockets.

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
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from vnedge.risk.risk_manager import MarketState

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_ERRORS = 5
_BACKOFF_SECONDS = 2.0


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
        self.symbol = symbol
        self.timeframe = timeframe
        self.slippage_est_bps = slippage_est_bps
        self.funding_refresh_seconds = funding_refresh_seconds

        self.closed_candles: asyncio.Queue[list] = asyncio.Queue()
        self.quote: tuple[float, float] | None = None  # (bid, ask)
        self.funding_rate: float = 0.0
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
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_error("funding", exc)
            await asyncio.sleep(self.funding_refresh_seconds)
