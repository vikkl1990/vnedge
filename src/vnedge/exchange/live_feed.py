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
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from vnedge.data.ccxt_client import create_ccxt_async_exchange
from vnedge.data.schemas import TIMEFRAME_MS
from vnedge.exchange.book_imbalance import (
    BookImbalance,
    BookTape,
    imbalance_l1,
    imbalance_l2,
)
from vnedge.exchange.heartbeat import HeartbeatStatus
from vnedge.risk.risk_manager import MarketState

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_ERRORS = 5
_BACKOFF_SECONDS = 2.0
_DEFAULT_REST_CANDLE_POLL_SECONDS = 10.0
_DEFAULT_REST_QUOTE_POLL_SECONDS = 2.0
_VALIDATED_CCXT_PRO_FEEDS = {"binanceusdm", "bybit"}
# Short, bounded event history for quote-held acceptance. At 100 BBO updates/s
# this retains roughly twenty seconds -- comfortably beyond the current 3-5s
# hold contracts -- without turning the public feed into an unbounded tape.
QUOTE_ACCEPTANCE_BUFFER_SIZE = 2_048
_QUOTE_OVERFLOW_ATTR = "_vnedge_quote_overflow_drops"


@dataclass(frozen=True, slots=True)
class QuoteUpdate:
    """One executable top-of-book observation for shadow acceptance.

    ``ts`` is the market-event clock used by causal acceptance.  Venues that
    publish an event timestamp set ``exchange_timestamped=True``; otherwise
    the publisher explicitly falls back to the local receipt clock.  Keeping
    ``received_ts`` separately makes transport lag observable instead of
    silently stretching a five-second acceptance hold.
    """

    ts: datetime
    bid: float
    ask: float
    received_ts: datetime | None = None
    sequence: int | str | None = None
    source: str = "unknown"
    exchange_timestamped: bool = False
    # Present only when the exact consumed quote came from a sized book frame.
    # Heartbeats, funding, and unsized ticker frames never populate it.
    book: BookImbalance | None = None

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None or self.ts.utcoffset() is None:
            raise ValueError("quote event timestamp must be timezone-aware")
        received = self.received_ts or self.ts
        if received.tzinfo is None or received.utcoffset() is None:
            raise ValueError("quote receive timestamp must be timezone-aware")
        if not math.isfinite(self.bid) or self.bid <= 0:
            raise ValueError("quote bid must be finite and positive")
        if not math.isfinite(self.ask) or self.ask <= 0:
            raise ValueError("quote ask must be finite and positive")
        if self.ask < self.bid:
            raise ValueError("quote ask must not be below bid")
        if self.sequence is not None and (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, (int, str))
        ):
            raise ValueError("quote sequence must be an integer, string, or None")
        if not self.source.strip():
            raise ValueError("quote source cannot be empty")
        if self.book is not None and (
            not math.isclose(self.book.bid, self.bid, rel_tol=0.0, abs_tol=1e-12)
            or not math.isclose(self.book.ask, self.ask, rel_tol=0.0, abs_tol=1e-12)
        ):
            raise ValueError("quote and sized-book top of book must match")
        object.__setattr__(self, "ts", self.ts.astimezone(UTC))
        object.__setattr__(self, "received_ts", received.astimezone(UTC))

    @property
    def ingest_lag_seconds(self) -> float:
        received = self.received_ts or self.ts
        return max(0.0, (received - self.ts).total_seconds())


def _event_datetime(raw: object) -> datetime | None:
    """Normalize common seconds/ms/us/ns venue timestamps to aware UTC."""
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    absolute = abs(value)
    if absolute >= 1e17:  # nanoseconds
        value /= 1_000_000_000
    elif absolute >= 1e14:  # microseconds
        value /= 1_000_000
    elif absolute >= 1e11:  # milliseconds
        value /= 1_000
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _publish_latest_quote(
    queue: asyncio.Queue[QuoteUpdate], *, bid: float, ask: float,
    event_ts: datetime | None = None,
    received_ts: datetime | None = None,
    sequence: int | str | None = None,
    source: str = "unknown",
    book: BookImbalance | None = None,
) -> QuoteUpdate:
    """Publish into a bounded acceptance history without blocking ingress.

    Overflow is explicit on the queue instead of silently replacing an
    observation. Consumers reset any in-flight hold when the counter advances.
    """
    received = received_ts or datetime.now(UTC)
    normalized_sequence = (
        sequence
        if isinstance(sequence, (int, str)) and not isinstance(sequence, bool)
        else None
    )
    update = QuoteUpdate(
        ts=event_ts or received,
        bid=bid,
        ask=ask,
        received_ts=received,
        sequence=normalized_sequence,
        source=source,
        exchange_timestamped=event_ts is not None,
        book=book,
    )
    if queue.full():
        try:
            queue.get_nowait()
            record_quote_overflow(queue)
        except asyncio.QueueEmpty:  # pragma: no cover - another task drained it
            pass
    queue.put_nowait(update)
    return update


def quote_overflow_drops(queue: asyncio.Queue[QuoteUpdate]) -> int:
    """Number of quote observations evicted from this bounded queue."""
    return int(getattr(queue, _QUOTE_OVERFLOW_ATTR, 0))


def record_quote_overflow(queue: asyncio.Queue[QuoteUpdate]) -> None:
    """Increment the explicit eviction counter attached to a quote queue."""
    queue.__dict__[_QUOTE_OVERFLOW_ATTR] = quote_overflow_drops(queue) + 1


def _advance_forming(
    current: list | None, update: list
) -> tuple[list, list | None]:
    """Apply one OHLCV update without regressing on reconnect replays.

    Returns ``(forming, newly_closed)``. Once a newer interval has been seen,
    an exchange replay of an older row cannot replace it or close it again.
    """
    if current is None:
        return update, None
    if update[0] < current[0]:
        return current, None
    if update[0] > current[0]:
        return update, current
    return update, None


def _data_quality(
    last_event_at: datetime | None,
    healthy: bool,
    silence_limit_s: float,
) -> tuple[bool, str, str]:
    if last_event_at is None:
        return True, "stale", "market stream has not produced an event"
    age = (datetime.now(UTC) - last_event_at).total_seconds()
    if age > silence_limit_s:
        return True, "stale", f"market stream silent for {age:.1f}s"
    if not healthy:
        return True, "degraded", "market transport unhealthy"
    return False, "ok", ""


class LiveMarketFeed:
    def __init__(
        self,
        exchange_id: str = "binanceusdm",
        *,
        symbol: str,
        timeframe: str = "1m",
        slippage_est_bps: float = 2.0,
        funding_refresh_seconds: float = 900.0,
        data_silence_seconds: float = 60.0,
        enable_candles: bool = True,
        enable_quotes: bool = True,
        enable_funding: bool = True,
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
        if data_silence_seconds <= 0:
            raise ValueError("data_silence_seconds must be positive")
        self.data_silence_seconds = data_silence_seconds
        self.enable_candles = enable_candles
        self.enable_quotes = enable_quotes
        self.enable_funding = enable_funding
        if not (enable_candles or enable_quotes or enable_funding):
            raise ValueError("market feed must enable at least one stream")

        self.closed_candles: asyncio.Queue[list] = asyncio.Queue()
        self.quote_updates: asyncio.Queue[QuoteUpdate] = asyncio.Queue(
            maxsize=QUOTE_ACCEPTANCE_BUFFER_SIZE
        )
        self.quote: tuple[float, float] | None = None  # (bid, ask)
        self.funding_rate: float = 0.0
        # SETTLED funding prints [(ts_ms, rate), ...] refreshed with the rate.
        # Strategies validated on settled-print series (funding-MR) must read
        # THIS, not funding_rate: the predicted rate is a different series
        # than research used, and mixing them silently shifts percentiles.
        self.funding_events: list[tuple[int, float]] = []
        self.book_metrics: dict | None = None  # live L2 metrics (fast loop)
        self.last_event_at: datetime | None = None
        self.healthy: bool = False
        self.candles_closed = 0
        self._consecutive_errors = 0
        self._forming: list | None = None
        self._tasks: list[asyncio.Task] = []
        self._last_book_metrics_at = 0.0

    # --- Lifecycle ----------------------------------------------------------------
    async def start(self) -> None:
        self._tasks = []
        if getattr(self, "enable_candles", True):
            self._tasks.append(asyncio.create_task(self._watch_candles(), name="feed-candles"))
        if getattr(self, "enable_funding", True):
            self._tasks.append(asyncio.create_task(self._refresh_funding(), name="feed-funding"))
        if getattr(self, "enable_quotes", True):
            self._tasks.append(asyncio.create_task(self._watch_book(), name="feed-book"))

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
        degraded, quality, reason = _data_quality(
            self.last_event_at,
            self.healthy,
            self.data_silence_seconds,
        )
        return MarketState(
            symbol=self.symbol,
            last_update=self.last_event_at or datetime(1970, 1, 1, tzinfo=UTC),
            spread_bps=spread_bps,
            estimated_slippage_bps=self.slippage_est_bps,
            funding_rate=self.funding_rate,
            exchange_healthy=self.healthy,
            data_degraded=degraded,
            data_quality=quality,
            data_quality_reason=reason,
        )

    @property
    def forming_candle(self) -> list | None:
        """The currently-forming (not-yet-closed) candle row, or None.

        Read-only awareness for the Time Machine. Never used for decisions — the
        strategy path consumes only closed candles from ``closed_candles``.
        """
        return self._forming

    # --- Stream loops -----------------------------------------------------------------
    async def _watch_candles(self) -> None:
        while True:
            try:
                rows = await self._ex.watch_ohlcv(self.symbol, self.timeframe)
                self._mark_ok()
                for row in rows:
                    self._forming, newly_closed = _advance_forming(self._forming, row)
                    if newly_closed is not None:
                        # a newer interval started: the forming candle is closed
                        await self.closed_candles.put(newly_closed)
                        self.candles_closed += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect with backoff
                self._mark_error("candles", exc)
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

    async def _watch_book(self) -> None:
        """Single order-book subscription for quotes and throttled L2 metrics.

        Uses the venue-safe depth limit (Bybit rejects anything but
        {1,50,200,1000}). Every update publishes executable BBO immediately;
        the heavier dashboard metrics are recomputed at most once per second.
        """
        while True:
            try:
                ob = await self._ex.watch_order_book(self.symbol, limit=50)
                if ob.get("bids") and ob.get("asks"):
                    bid = float(ob["bids"][0][0])
                    ask = float(ob["asks"][0][0])
                    if 0 < bid <= ask:
                        self.quote = (bid, ask)
                        _publish_latest_quote(
                            self.quote_updates,
                            bid=bid,
                            ask=ask,
                            event_ts=_event_datetime(ob.get("timestamp")),
                            sequence=ob.get("nonce"),
                            source=f"{self.exchange_id}:watch_order_book",
                        )
                        self._mark_ok()
                    loop_now = asyncio.get_running_loop().time()
                    if loop_now - self._last_book_metrics_at >= 1.0:
                        self.book_metrics = compute_book_metrics(
                            self.symbol, ob["bids"], ob["asks"]
                        )
                        self._last_book_metrics_at = loop_now
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_error("book", exc)
                await asyncio.sleep(_BACKOFF_SECONDS)


def compute_book_metrics(symbol: str, bids: list, asks: list) -> dict | None:
    """Flatten a bids/asks ladder into the fast-loop L2 metrics dict."""
    from vnedge.scalping.depth import OrderBookL2

    try:
        book = OrderBookL2(
            symbol=symbol,
            bids=tuple((float(p), float(q)) for p, q, *_ in bids[:10]),
            asks=tuple((float(p), float(q)) for p, q, *_ in asks[:10]),
            event_time=datetime.now(UTC),
        )
    except (ValueError, TypeError):
        return None  # crossed/empty snapshot — keep the last good metrics
    return {
        "spread_bps": round(book.spread_bps, 4),
        "imbalance": round(book.depth_imbalance(), 4),
        "liq_usd_5bps": round(book.liquidity_usd_within_bps(5.0), 0),
        "ts": datetime.now(UTC).isoformat(),
    }


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
        data_silence_seconds: float = 60.0,
        enable_candles: bool = True,
        enable_quotes: bool = True,
        enable_funding: bool = True,
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
        if data_silence_seconds <= 0:
            raise ValueError("data_silence_seconds must be positive")
        self.data_silence_seconds = data_silence_seconds
        self.enable_candles = enable_candles
        self.enable_quotes = enable_quotes
        self.enable_funding = enable_funding
        if not (enable_candles or enable_quotes or enable_funding):
            raise ValueError("market feed must enable at least one stream")

        self.closed_candles: asyncio.Queue[list] = asyncio.Queue()
        self.quote_updates: asyncio.Queue[QuoteUpdate] = asyncio.Queue(
            maxsize=QUOTE_ACCEPTANCE_BUFFER_SIZE
        )
        self.quote: tuple[float, float] | None = None
        self.funding_rate: float = 0.0
        self.funding_events: list[tuple[int, float]] = []  # settled prints (ts_ms, rate)
        self.book_metrics: dict | None = None  # L2 metrics (native-ws subclasses)
        self.last_event_at: datetime | None = None
        self.last_transport_at: datetime | None = None
        self.healthy: bool = False
        self.candles_closed = 0
        self._consecutive_errors = 0
        self._last_emitted_candle_ts: int | None = None
        self._forming: list | None = None
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = []
        if self.enable_candles:
            self._tasks.append(asyncio.create_task(self._poll_candles(), name="rest-feed-candles"))
        if self.enable_quotes:
            self._tasks.append(asyncio.create_task(self._poll_quotes(), name="rest-feed-quotes"))
        if self.enable_funding:
            self._tasks.append(asyncio.create_task(self._refresh_funding(), name="rest-feed-funding"))

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
        degraded, quality, reason = _data_quality(
            self.last_event_at,
            self.healthy,
            self.data_silence_seconds,
        )
        return MarketState(
            symbol=self.symbol,
            last_update=self.last_event_at or datetime(1970, 1, 1, tzinfo=UTC),
            spread_bps=spread_bps,
            estimated_slippage_bps=self.slippage_est_bps,
            funding_rate=self.funding_rate,
            exchange_healthy=self.healthy,
            data_degraded=degraded,
            data_quality=quality,
            data_quality_reason=reason,
        )

    @property
    def forming_candle(self) -> list | None:
        """Read-only forming bar; never enters the closed-candle decision queue."""
        return list(self._forming) if self._forming is not None else None

    def prime_closed_through(self, open_time_ms: int) -> None:
        """Seed the REST/WS dedupe watermark from the lane warm-up tail.

        A feed starts after REST warm-up has already supplied the most recent
        closed bar.  Without this handoff, its first fallback poll re-emits
        that historical bar as if it had just closed, contaminating live
        receipt latency and starting an impossible canonical-lake wait.
        Shared feeds may be primed by several equivalent lanes, so only the
        greatest proven timestamp is retained.
        """
        timestamp = int(open_time_ms)
        if self._last_emitted_candle_ts is None:
            self._last_emitted_candle_ts = timestamp
        else:
            self._last_emitted_candle_ts = max(
                self._last_emitted_candle_ts, timestamp
            )

    async def _poll_candles(self) -> None:
        while True:
            await self._poll_candles_once()
            await asyncio.sleep(self.candle_poll_seconds)

    async def _poll_candles_once(self) -> None:
        """One REST poll: fetch recent bars, emit the latest CLOSED one (if new)."""
        step_ms = TIMEFRAME_MS[self.timeframe]
        try:
            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            since = now_ms - 4 * step_ms
            rows = await self._ex.fetch_ohlcv(
                self.symbol, self.timeframe, since=since, limit=4
            )
            forming = [row for row in rows if int(row[0]) + step_ms > now_ms]
            self._forming = list(forming[-1]) if forming else None
            closed = self._latest_closed_row(rows, now_ms, step_ms)
            if closed is not None:
                self._emit_closed(closed)
            if rows:
                self._mark_ok()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._mark_error("candles", exc)
            await asyncio.sleep(_BACKOFF_SECONDS)

    def _emit_closed(self, row: list) -> bool:
        """Queue a closed candle if it is newer than the last emitted one.

        The monotonic guard deduplicates across sources (e.g. a websocket
        candle stream and its REST fallback emitting the same bar).
        """
        ts = int(row[0])
        if self._last_emitted_candle_ts is not None and ts <= self._last_emitted_candle_ts:
            return False
        self.closed_candles.put_nowait(list(row))
        self._last_emitted_candle_ts = ts
        self.candles_closed += 1
        return True

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
                        _publish_latest_quote(
                            self.quote_updates,
                            bid=bid,
                            ask=ask,
                            event_ts=_event_datetime(book.get("timestamp")),
                            sequence=book.get("nonce"),
                            source=f"{self.exchange_id}:fetch_order_book",
                        )
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
    """Delta India feed: native websocket candles/quotes plus REST settlement history.

    Delta has no CCXT Pro class, but its native public websocket
    (``DeltaPublicWsClient``) pushes everything the lane needs: top-of-book
    from ``ob_l1``, indicative funding from ``ticker``, and
    closed candles from the ``candlestick_<timeframe>`` channel (verified
    live 2026-07-08 — the channel streams the forming candle; the client
    emits it as closed when a newer ``candle_start_time`` appears, the same
    bar-close discipline as ``LiveMarketFeed``).

    REST candle polling remains as a FALLBACK only: it emits a closed bar
    when the websocket has not delivered one for 2x the timeframe (or has
    not delivered any yet, e.g. right after startup mid-interval). The
    monotonic ``_emit_closed`` guard deduplicates across the two sources.
    Staleness mirrors the last websocket event, so the gateway's freshness
    check reflects the real stream.
    """

    def __init__(
        self,
        exchange_id: str,
        *,
        symbol: str,
        timeframe: str = "1m",
        slippage_est_bps: float = 3.0,
        candle_poll_seconds: float = _DEFAULT_REST_CANDLE_POLL_SECONDS,
        enable_candles: bool = True,
        enable_quotes: bool = True,
        enable_funding: bool = True,
        enable_l2: bool = False,
        l2_levels: int = 5,
        l2_decay_k: float = 0.35,
    ) -> None:
        super().__init__(
            exchange_id,
            symbol=symbol,
            timeframe=timeframe,
            slippage_est_bps=slippage_est_bps,
            candle_poll_seconds=candle_poll_seconds,
            enable_candles=enable_candles,
            enable_quotes=enable_quotes,
            enable_funding=enable_funding,
        )
        from vnedge.exchange.delta_ws import DeltaPublicWsClient, delta_native_symbol

        self.feed_mode = "delta native ws candles (rest fallback)"
        self._native_symbol = delta_native_symbol(symbol)
        self._last_ws_candle_at: datetime | None = None
        self._book_tape = BookTape(stale_after_s=2.0)
        self._l2_book_tape = BookTape(stale_after_s=2.0)
        if l2_levels <= 0:
            raise ValueError("Delta L2 levels must be positive")
        if not math.isfinite(l2_decay_k) or l2_decay_k < 0:
            raise ValueError("Delta L2 decay must be finite and non-negative")
        self._enable_l2 = bool(enable_l2)
        self._l2_levels = int(l2_levels)
        self._l2_decay_k = float(l2_decay_k)
        channels: tuple[str, ...] = ()
        if enable_quotes:
            channels += ("ticker",)
        if enable_quotes:
            channels += ("ob_l1",)
        if enable_quotes and self._enable_l2:
            # Deliberately opt-in until captured production frames prove the
            # compact ob_l2 decoder and live/replay sequence parity.
            channels += ("ob_l2",)
        if enable_funding:
            channels += ("funding_rate",)
        self._ws = DeltaPublicWsClient(
            [self._native_symbol],
            channels=channels,
            candle_timeframes=(timeframe,) if enable_candles else (),
            on_book=self._on_ws_book,
            on_candle=self._on_ws_candle,
        )

    async def start(self) -> None:
        await self._ws.start()
        self._tasks = [asyncio.create_task(self._sync_ws_state(), name="delta-feed-ws-sync")]
        if self.enable_candles:
            self._tasks.append(
                asyncio.create_task(self._poll_candles(), name="delta-feed-candles")
            )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._ws.stop()
        await self._ex.close()

    # --- websocket candles with REST fallback ------------------------------------
    def _on_ws_candle(self, sym: str, timeframe: str, row: list) -> None:
        """A candle CLOSED on the native websocket stream."""
        if sym != self._native_symbol or timeframe != self.timeframe:
            return
        self._last_ws_candle_at = datetime.now(UTC)
        self._emit_closed(row)

    def _on_ws_book(self, sym: str, buy: list, sell: list, message: dict) -> None:
        """Publish every sized Delta book frame; never poll/coalesce L1.

        ``ticker`` can carry an unsized BBO and is useful for telemetry, but it
        cannot prove imbalance. Only ``ob_l1`` (and a future explicitly enabled
        L2 channel) enters the acceptance tape.
        """
        if sym != self._native_symbol or message.get("type") not in {
            "ob_l1",
            "l2_orderbook",
            "ob_l2",
        }:
            return
        if not buy or not sell:
            return
        try:
            bid = float(buy[0]["limit_price"])
            ask = float(sell[0]["limit_price"])
            bid_size = float(buy[0]["size"])
            ask_size = float(sell[0]["size"])
            from vnedge.exchange.venue_specs import frozen_delta_specs

            spec = frozen_delta_specs().get(self._native_symbol)
            tick = float(spec.tick_size) if spec is not None and spec.tick_size else 0.0
        except (KeyError, TypeError, ValueError):
            return
        event_ts = self._ws.book_event_at.get(self._native_symbol)
        if event_ts is None:
            return
        message_type = str(message.get("type") or "")
        if message_type == "ob_l1":
            snapshot = imbalance_l1(
                bid,
                ask,
                bid_size,
                ask_size,
                tick=tick,
                ts=event_ts,
            )
            tape = self._book_tape
            source = "delta_india:ob_l1"
        else:
            if not self._enable_l2:
                return
            try:
                bids = [
                    (float(level["limit_price"]), float(level["size"]))
                    for level in buy
                ]
                asks = [
                    (float(level["limit_price"]), float(level["size"]))
                    for level in sell
                ]
            except (KeyError, TypeError, ValueError):
                return
            snapshot = imbalance_l2(
                bids,
                asks,
                tick=tick,
                ts=event_ts,
                levels=self._l2_levels,
                decay_k=self._l2_decay_k,
            )
            tape = self._l2_book_tape
            source = "delta_india:ob_l2"
        if snapshot is None:
            return
        tape.on_book(snapshot)
        now = datetime.now(UTC)
        live_snapshot = tape.live(now)
        if live_snapshot is None:
            # Never extend stale L2. A separately arriving L1 frame remains
            # authoritative and will publish on its own event clock.
            return
        self.quote = (live_snapshot.bid, live_snapshot.ask)
        _publish_latest_quote(
            self.quote_updates,
            bid=live_snapshot.bid,
            ask=live_snapshot.ask,
            event_ts=event_ts,
            received_ts=now,
            sequence=self._ws.book_sequence.get(self._native_symbol),
            source=source,
            book=live_snapshot,
        )

    def _ws_candles_fresh(self, now: datetime | None = None) -> bool:
        """Websocket candle stream considered alive: a close within 2x timeframe."""
        if self._last_ws_candle_at is None:
            return False
        age = ((now or datetime.now(UTC)) - self._last_ws_candle_at).total_seconds()
        return age < 2.0 * (TIMEFRAME_MS[self.timeframe] / 1000.0)

    async def _poll_candles(self) -> None:
        # FALLBACK-only loop: while websocket candles flow, REST stays quiet.
        while True:
            if not self._ws_candles_fresh():
                await self._poll_candles_once()
            await asyncio.sleep(self.candle_poll_seconds)

    async def _sync_ws_state(self) -> None:
        """Mirror native websocket state into the polling-feed surface."""
        while True:
            try:
                fr = self._ws.funding_rate.get(self._native_symbol)
                if fr is not None:
                    self.funding_rate = fr
                # Current funding remains telemetry. Only a venue schedule
                # rollover produces a settled cash event for the ledger.
                self.funding_events = list(
                    self._ws.settled_funding_events.get(self._native_symbol, ())
                )
                native_forming = self._ws.forming_candle(
                    self._native_symbol, self.timeframe
                )
                if native_forming is not None:
                    self._forming = native_forming
                book = self._ws.books.get(self._native_symbol)
                if book:
                    buy, sell = book
                    metrics = compute_book_metrics(
                        self.symbol,
                        [[e["limit_price"], e["size"]] for e in buy[:10]],
                        [[e["limit_price"], e["size"]] for e in sell[:10]],
                    )
                    if metrics is not None:
                        self.book_metrics = metrics
                # honest staleness/health: track the real stream, not this loop
                if self._ws.last_event_at is not None:
                    self.last_event_at = self._ws.last_event_at
                if self._ws.last_transport_at is not None:
                    self.last_transport_at = self._ws.last_transport_at
                status = self._ws.heartbeat_status()
                self.healthy = self._ws.healthy and status not in {
                    HeartbeatStatus.PONG_TIMEOUT,
                    HeartbeatStatus.TRANSPORT_STALE,
                    HeartbeatStatus.DATA_STALE,
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._mark_error("ws-sync", exc)
            await asyncio.sleep(0.5)

    def market_state(self) -> MarketState:
        state = super().market_state()
        status = self._ws.heartbeat_status()
        if status in {
            HeartbeatStatus.PONG_TIMEOUT,
            HeartbeatStatus.TRANSPORT_STALE,
            HeartbeatStatus.DATA_STALE,
        }:
            return replace(
                state,
                exchange_healthy=False,
                data_degraded=True,
                data_quality="stale",
                data_quality_reason=f"delta websocket {status.value}",
            )
        return state


def supports_ccxt_pro_feed(exchange_id: str) -> bool:
    """Whether this CCXT id has the websocket methods our live feed needs."""
    try:
        import ccxt.pro as ccxtpro  # heavy import kept local
    except Exception:  # noqa: BLE001  # pragma: no cover - environment-specific
        return False
    return exchange_id in _VALIDATED_CCXT_PRO_FEEDS and hasattr(ccxtpro, exchange_id)


_DELTA_NATIVE_WS_IDS = {"delta_india", "delta", "deltaindia"}


def create_market_feed(
    exchange_id: str,
    *,
    symbol: str,
    timeframe: str = "1m",
    enable_candles: bool = True,
    enable_quotes: bool = True,
    enable_funding: bool = True,
    enable_l2: bool = False,
) -> LiveMarketFeed | RestPollingMarketFeed:
    if supports_ccxt_pro_feed(exchange_id):
        return LiveMarketFeed(
            exchange_id,
            symbol=symbol,
            timeframe=timeframe,
            enable_candles=enable_candles,
            enable_quotes=enable_quotes,
            enable_funding=enable_funding,
        )
    if exchange_id in _DELTA_NATIVE_WS_IDS:
        # Delta has no CCXT Pro class but does have a native public websocket.
        return DeltaWsFeed(
            exchange_id,
            symbol=symbol,
            timeframe=timeframe,
            enable_candles=enable_candles,
            enable_quotes=enable_quotes,
            enable_funding=enable_funding,
            enable_l2=enable_l2,
        )
    feed = RestPollingMarketFeed(
        exchange_id,
        symbol=symbol,
        timeframe=timeframe,
        enable_candles=enable_candles,
        enable_quotes=enable_quotes,
        enable_funding=enable_funding,
    )
    logger.warning(
        "%s has no validated websocket feed; using explicit REST polling mode",
        exchange_id,
    )
    return feed
