"""Native Delta Exchange India production public-websocket client.

The only accepted host is ``wss://public-socket.india.delta.exchange``.
Subscriptions use the current public channel names (``ticker``, ``ob_l1``,
``trades``, and ``candlestick_<tf>``). Delta server heartbeats and RFC pongs
advance transport liveness only; market freshness has independent book, trade,
and candle clocks. Public data only: no authentication, orders, or dead-man
heartbeat exists in this process.

Legacy frame names remain decode-compatible during migration, but this client
never subscribes to them. Candle messages are forming updates; a newer
``candle_start_time`` proves the previous interval closed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from vnedge.data.symbols import canonical_symbol
from vnedge.exchange.delta_limit_state import WsConnectBreaker, parse_rate_limit_reset
from vnedge.exchange.heartbeat import (
    HeartbeatConfig,
    HeartbeatStatus,
    ReconnectBackoff,
    WsHeartbeat,
)

logger = logging.getLogger(__name__)

DELTA_INDIA_WS_URL = "wss://public-socket.india.delta.exchange"
ALLOWED_PUBLIC_WS_HOSTS = frozenset({"public-socket.india.delta.exchange"})

DEFAULT_CHANNELS = ("ticker", "ob_l1", "trades", "funding_rate")

_MAX_CONSECUTIVE_ERRORS = 5


def _normalise_book_side(raw: object) -> list[dict[str, object]]:
    """Normalize verbose and compact Delta levels into one internal shape."""
    if not isinstance(raw, list):
        return []
    levels: list[dict[str, object]] = []
    for item in raw:
        if isinstance(item, dict):
            price = item.get("limit_price")
            if price is None:
                price = item.get("price") or item.get("p")
            size = item.get("size")
            if size is None:
                size = item.get("s") or item.get("quantity")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price, size = item[0], item[1]
        else:
            continue
        if price is None or size is None:
            continue
        levels.append({"limit_price": price, "size": size})
    return levels


def delta_native_symbol(symbol: str) -> str:
    """Convert a CCXT-style Delta symbol to Delta's native ticker.

    ``BTC/USD:USD`` -> ``BTCUSD``. Already-native symbols pass through.
    """
    return canonical_symbol(symbol)


def assert_delta_public_ws_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "wss" or parsed.hostname not in ALLOWED_PUBLIC_WS_HOSTS:
        raise ValueError(f"refusing Delta public websocket host: {url}")
    return url


class DeltaPublicWsClient:
    """Native public websocket client for Delta Exchange India.

    Reader task connects, subscribes to the requested public channels, and
    updates live per-symbol state. Reconnects with bounded backoff. All state
    is best-effort and last-known-value; freshness is exposed via
    ``last_event_at`` so callers can compute honest staleness.
    """

    def __init__(
        self,
        symbols: list[str],
        *,
        channels: tuple[str, ...] = DEFAULT_CHANNELS,
        candle_timeframes: tuple[str, ...] = (),
        url: str = DELTA_INDIA_WS_URL,
        connect: Callable[..., Any] | None = None,
        on_book: Callable[[str, list, list, dict], None] | None = None,
        on_trade: Callable[[str, dict], None] | None = None,
        on_candle: Callable[[str, str, list], None] | None = None,
        heartbeat: HeartbeatConfig | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        breaker: WsConnectBreaker | None = None,
    ) -> None:
        self.symbols = [delta_native_symbol(s) for s in symbols]
        self.channels = tuple(channels) + tuple(
            f"candlestick_{tf}" for tf in candle_timeframes
        )
        self.url = assert_delta_public_ws_url(url)
        self._connect = connect  # injectable for tests; defaults to websockets.connect
        self.on_book = on_book
        self.on_trade = on_trade
        # on_candle(symbol, timeframe, [ts_ms, o, h, l, c, v]) — CLOSED candles only
        self.on_candle = on_candle
        self.heartbeat_config = heartbeat or HeartbeatConfig(
            ping_interval_s=20.0,
            pong_timeout_s=20.0,
            transport_silence_s=35.0,
            data_silence_s=60.0,
            use_ws_control_ping=False,  # websockets handles RFC ping/pong
            use_app_ping=False,  # Delta sends heartbeats after enable_heartbeat
        )
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._heartbeat = WsHeartbeat(
            self.heartbeat_config,
            started_at=self._monotonic(),
        )
        self._backoff = ReconnectBackoff()
        self._breaker = breaker or WsConnectBreaker(
            Path(os.environ.get(
                "VNEDGE_DELTA_WS_BREAKER_PATH", "data/state/delta_ws_breaker.json"
            ))
        )
        self._breaker.load()

        # live per-symbol state (native symbol -> value)
        self.best_bid: dict[str, float] = {}
        self.best_ask: dict[str, float] = {}
        # Per-symbol provenance for quote-held scanner decisions. Delta book
        # messages may expose either an explicit sequence or only a timestamp;
        # retain both separately from local socket receipt time.
        self.book_event_at: dict[str, datetime] = {}
        self.book_sequence: dict[str, int | str] = {}
        self.funding_rate: dict[str, float] = {}
        # Funding is booked only after Delta rolls the venue-provided next
        # realization timestamp forward.  The current rate is telemetry until
        # then; elapsed wall time never fabricates a settlement.
        self.next_funding_at_ms: dict[str, int] = {}
        self.settled_funding_events: dict[str, list[tuple[int, float]]] = {}
        self._funding_schedule: dict[str, tuple[int, float]] = {}
        self.mark_price: dict[str, float] = {}
        self.books: dict[str, tuple[list, list]] = {}
        self.last_trade: dict[str, dict] = {}
        # candle state per (symbol, timeframe): forming candle + last closed
        self._forming_candles: dict[tuple[str, str], list] = {}
        self.last_closed_candle: dict[tuple[str, str], list] = {}

        self.last_event_at: datetime | None = None
        self.last_transport_at: datetime | None = None
        self.last_book_at: datetime | None = None
        self.last_trade_at: datetime | None = None
        self.last_candle_at: datetime | None = None
        self.healthy: bool = False
        self._consecutive_errors = 0
        self._closed = False
        self._task: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._closed = False
        self._task = asyncio.create_task(self._run(), name="delta-ws-reader")

    async def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def _subscribe_msg(self) -> dict:
        return {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {"name": name, "symbols": list(self.symbols)}
                    for name in self.channels
                ]
            },
        }

    async def _run(self) -> None:
        connect = self._connect or _default_connect
        while not self._closed:
            cooldown = self._breaker.remaining(self._wall_clock())
            if cooldown > 0:
                self.healthy = False
                await asyncio.sleep(cooldown)
                continue
            try:
                async with connect(self.url) as ws:
                    self._heartbeat.reset(self._monotonic())
                    # Enable the official server heartbeat immediately.  It is
                    # transport liveness only and never refreshes book/trade age.
                    await ws.send(json.dumps({"type": "enable_heartbeat"}))
                    await ws.send(json.dumps(self._subscribe_msg()))
                    iterator = ws.__aiter__()
                    while not self._closed:
                        try:
                            raw = await asyncio.wait_for(
                                anext(iterator),
                                timeout=self.heartbeat_config.transport_silence_s,
                            )
                        except StopAsyncIteration:
                            break
                        self._handle_raw(raw)
                if not self._closed:
                    self.healthy = False
                    self._mark_error(ConnectionError("delta websocket stream ended"))
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self.healthy = False
                self._mark_error(TimeoutError("delta websocket transport silent"))
            except Exception as exc:  # noqa: BLE001 - reconnect on any stream error
                self.healthy = False
                if _handshake_status(exc) == 429:
                    reset_at = _handshake_reset_at(exc, now=self._wall_clock())
                    self._breaker.on_handshake_429(
                        self._wall_clock(), reset_at=reset_at
                    )
                    self._mark_error(RuntimeError("Delta websocket HTTP 429; cooldown persisted"))
                    continue
                self._mark_error(exc)
            # normal stream-end or error: reconnect with bounded backoff so we
            # never hot-loop when the socket closes cleanly.
            if self._closed:
                break
            await asyncio.sleep(self._backoff.next_delay())

    # -- message handling --------------------------------------------------
    def _handle_raw(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(msg, dict):
            self._touch_transport()
            self._handle(msg)

    def _handle(self, msg: dict) -> None:
        mtype = msg.get("type")
        sym = msg.get("symbol") or msg.get("sy")
        if mtype in {"ob_l1", "ob_l2", "l2_orderbook"}:
            self._handle_book(sym, msg)
        elif mtype in {"trades", "all_trades"}:
            self._handle_trade(sym, msg)
        elif mtype == "funding_rate":
            self._handle_funding(sym, msg)
        elif mtype in {"ticker", "v2/ticker"}:
            self._handle_ticker(sym, msg)
        elif isinstance(mtype, str) and mtype.startswith("candlestick_"):
            self._handle_candle(sym, mtype, msg)
        elif mtype == "heartbeat":
            self._touch_transport()
        elif mtype == "pong":
            self._touch_transport(pong=True)
        # subscriptions / errors and unknown types carry transport liveness only

    def _handle_book(self, sym: str | None, msg: dict) -> None:
        if not sym:
            return
        buy = _normalise_book_side(msg.get("buy") or msg.get("bids") or msg.get("b"))
        sell = _normalise_book_side(msg.get("sell") or msg.get("asks") or msg.get("a"))
        # Compact India ob_l1 frames: bp/ap are prices, bs/as are contract sizes.
        if not buy and msg.get("bp") is not None:
            buy = [{"limit_price": msg["bp"], "size": msg.get("bs", 0)}]
        if not sell and msg.get("ap") is not None:
            sell = [{"limit_price": msg["ap"], "size": msg.get("as", 0)}]
        if not buy and msg.get("best_bid") is not None:
            buy = [{"limit_price": msg["best_bid"], "size": msg.get("bid_size", 0)}]
        if not sell and msg.get("best_ask") is not None:
            sell = [{"limit_price": msg["best_ask"], "size": msg.get("ask_size", 0)}]
        if buy:
            self.best_bid[sym] = float(buy[0]["limit_price"])
        if sell:
            self.best_ask[sym] = float(sell[0]["limit_price"])
        # lts is the venue's last-book-update event clock; ts is publish time.
        event_at = self._message_datetime(
            msg.get("lts") or msg.get("timestamp") or msg.get("ts")
        )
        if event_at is not None:
            self.book_event_at[sym] = event_at
        sequence = (
            msg.get("sequence_no")
            or msg.get("sequence")
            or msg.get("nonce")
            or msg.get("lts")
            or msg.get("timestamp")
            or msg.get("ts")
        )
        if isinstance(sequence, (int, str)) and not isinstance(sequence, bool):
            self.book_sequence[sym] = sequence
        self.books[sym] = (buy, sell)
        self.last_book_at = self._now()
        self._touch()
        if self.on_book is not None:
            self.on_book(sym, buy, sell, msg)

    def _handle_trade(self, sym: str | None, msg: dict) -> None:
        if not sym:
            return
        try:
            # Production India frames are compact: p=price, s=size in
            # contracts, t=event time (microseconds), r=seller role.
            price = float(msg["price"] if "price" in msg else msg["p"])
            size = float(msg["size"] if "size" in msg else msg["s"])
        except (KeyError, TypeError, ValueError):
            return
        # taker side drives aggressor; buyer taker => buy print, seller taker => sell
        seller_role = msg.get("seller_role")
        if seller_role is None:
            seller_role = {"t": "taker", "m": "maker"}.get(str(msg.get("r") or ""))
        taker = "sell" if seller_role == "taker" else "buy"
        ts_raw = msg.get("timestamp", msg.get("t"))
        ts_ms = int(ts_raw) // 1000 if ts_raw is not None else self._now_ms()
        trade = {
            "symbol": sym,
            "price": price,
            "size": size,
            "side": taker,
            "ts_ms": ts_ms,
            "trade_id": msg.get("trade_id") or msg.get("id"),
        }
        self.last_trade[sym] = trade
        self.last_trade_at = self._now()
        self._touch()
        if self.on_trade is not None:
            self.on_trade(sym, trade)

    def _handle_funding(self, sym: str | None, msg: dict) -> None:
        if not sym:
            return
        raw = msg.get("funding_rate")
        if raw is None:
            raw = msg.get("fr")
        if raw is None:
            return
        try:
            # Delta reports funding as a percent; normalise to a fraction.
            rate = float(raw) / 100.0
        except (TypeError, ValueError):
            return
        self.funding_rate[sym] = rate

        next_raw = msg.get("next_funding_realization")
        if next_raw is None:
            next_raw = msg.get("nfr")
        next_ms = self._timestamp_ms(next_raw)
        if next_ms is not None:
            previous = self._funding_schedule.get(sym)
            if previous is not None and next_ms > previous[0]:
                # The schedule rollover proves the prior realization passed.
                # Use its last observed rate, never the new estimate.
                events = self.settled_funding_events.setdefault(sym, [])
                settled = (previous[0], previous[1])
                if not events or events[-1] != settled:
                    events.append(settled)
                    del events[:-64]
            if previous is None or next_ms >= previous[0]:
                self._funding_schedule[sym] = (next_ms, rate)
                self.next_funding_at_ms[sym] = next_ms
        self._touch()

    def _handle_candle(self, sym: str | None, mtype: str, msg: dict) -> None:
        """Track the forming candle; emit it as CLOSED when the next one starts.

        Same bar-close discipline as ``LiveMarketFeed._watch_candles``: Delta
        streams only the forming candle, so a candle is proven closed exactly
        when a message carries a newer ``candle_start_time``.
        """
        if not sym:
            return
        # The production India public socket uses compact fields (captured
        # 2026-08-31): sy/res/cst/o/h/l/c/v.  Older fixtures and some Delta
        # surfaces use the verbose equivalents.  Both shapes describe the
        # same forming candle and must share this close-discipline path.
        timeframe = str(
            msg.get("resolution")
            or msg.get("res")
            or mtype.removeprefix("candlestick_")
        )
        try:
            start_ms = self._timestamp_ms(
                msg.get("candle_start_time", msg.get("cst"))
            )
            if start_ms is None:
                return
            row = [
                start_ms,
                float(msg.get("open", msg.get("o"))),
                float(msg.get("high", msg.get("h"))),
                float(msg.get("low", msg.get("l"))),
                float(msg.get("close", msg.get("c"))),
                float(msg.get("volume", msg.get("v")) or 0.0),
            ]
        except (TypeError, ValueError):
            return
        key = (sym, timeframe)
        forming = self._forming_candles.get(key)
        if forming is not None and row[0] > forming[0]:
            # a newer interval started: the forming candle is closed
            self.last_closed_candle[key] = forming
            if self.on_candle is not None:
                self.on_candle(sym, timeframe, forming)
        if forming is None or row[0] >= forming[0]:
            self._forming_candles[key] = row
        # older-start messages (out-of-order replays) never regress the forming
        # candle and never re-close an interval; they only count as liveness.
        self.last_candle_at = self._now()
        self._touch()

    def forming_candle(self, symbol: str, timeframe: str) -> list | None:
        """Return a defensive copy of the native forming candle for observability."""
        row = self._forming_candles.get((symbol, timeframe))
        return list(row) if row is not None else None

    def _handle_ticker(self, sym: str | None, msg: dict) -> None:
        if not sym:
            return
        mp = msg.get("mark_price")
        if mp is not None:
            with suppress(TypeError, ValueError):
                self.mark_price[sym] = float(mp)
        bid = msg.get("best_bid") or msg.get("bid") or msg.get("bid_price")
        ask = msg.get("best_ask") or msg.get("ask") or msg.get("ask_price")
        if bid is not None and ask is not None:
            with suppress(TypeError, ValueError):
                bid_f, ask_f = float(bid), float(ask)
                if 0 < bid_f <= ask_f:
                    self.best_bid[sym], self.best_ask[sym] = bid_f, ask_f
                    self.book_event_at[sym] = self._message_datetime(
                        msg.get("timestamp")
                    ) or self._now()
                    self.last_book_at = self._now()
                    if self.on_book is not None:
                        self.on_book(
                            sym,
                            [{"limit_price": str(bid), "size": msg.get("bid_size", 0)}],
                            [{"limit_price": str(ask), "size": msg.get("ask_size", 0)}],
                            msg,
                        )
        raw_funding = msg.get("funding_rate")
        if raw_funding is not None:
            with suppress(TypeError, ValueError):
                self.funding_rate[sym] = float(raw_funding) / 100.0
        self._touch()

    # -- helpers -----------------------------------------------------------
    def quote(self, symbol: str) -> tuple[float, float] | None:
        sym = delta_native_symbol(symbol)
        bid = self.best_bid.get(sym)
        ask = self.best_ask.get(sym)
        if bid is None or ask is None:
            return None
        if 0 < bid <= ask:
            return (bid, ask)
        return None

    def _touch(self) -> None:
        now = self._now()
        self.last_event_at = now
        self.last_transport_at = now
        self._heartbeat.on_market_message(self._monotonic())
        self._consecutive_errors = 0
        self._backoff.reset()
        self.healthy = True

    def _touch_transport(self, *, pong: bool = False) -> None:
        self.last_transport_at = self._now()
        now = self._monotonic()
        if pong:
            self._heartbeat.on_pong(now)
        else:
            self._heartbeat.on_transport_message(now)

    def heartbeat_status(self) -> HeartbeatStatus:
        return self._heartbeat.tick(self._monotonic())

    def _mark_error(self, exc: Exception) -> None:
        self._consecutive_errors += 1
        if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
            self.healthy = False
        logger.warning(
            "delta ws error (%d consecutive): %s", self._consecutive_errors, exc
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _now_ms() -> int:
        return int(datetime.now(UTC).timestamp() * 1000)

    @staticmethod
    def _message_datetime(raw: object) -> datetime | None:
        """Normalize Delta's seconds/ms/us/ns timestamps to aware UTC."""
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        absolute = abs(value)
        if absolute >= 1e17:
            value /= 1_000_000_000
        elif absolute >= 1e14:
            value /= 1_000_000
        elif absolute >= 1e11:
            value /= 1_000
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    @staticmethod
    def _timestamp_ms(raw: object) -> int | None:
        """Normalize seconds/ms/us/ns venue timestamps to epoch milliseconds."""
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        absolute = abs(value)
        if absolute >= 1e17:  # ns
            value /= 1_000_000
        elif absolute >= 1e14:  # us
            value /= 1_000
        elif absolute < 1e11:  # seconds
            value *= 1_000
        return int(value)


def _default_connect(url: str):
    """Real websocket connect, imported lazily so tests need no network dep."""
    import websockets  # local import: heavy, optional at import time

    # ping_interval keeps the protocol-level connection alive; Delta also has an
    # app-level heartbeat we enable after subscribe.
    return websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22)


def _handshake_status(exc: Exception) -> int | None:
    for value in (
        getattr(exc, "status_code", None),
        getattr(exc, "status", None),
        getattr(getattr(exc, "response", None), "status_code", None),
        getattr(getattr(exc, "response", None), "status", None),
    ):
        if isinstance(value, int):
            return value
    return None


def _handshake_reset_at(exc: Exception, *, now: float) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    try:
        return parse_rate_limit_reset(headers, now=now)
    except (TypeError, ValueError):
        return None
