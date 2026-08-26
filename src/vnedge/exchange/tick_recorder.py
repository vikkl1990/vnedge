"""Tick + L2 order-book recorder — zero-risk data collection.

    python -m vnedge.exchange.tick_recorder --symbols BTC/USDT:USDT --levels 10

Streams live trades and L2 order-book depth via CCXT Pro websockets and writes
them to per-flush Parquet shard files. NO execution, NO credentials, NO order
code — it only reads public streams and writes files. This is the data source
the true microstructure scalper backtest needs (candles can't approximate real
order flow); collect for a couple of weeks, then replay.

Book schema keeps the level-0 L1 columns (bid/bid_qty/ask/ask_qty) for
backward compatibility with the top-of-book replay engine, and adds the full
ladder as bid_px_i/bid_qty_i/ask_px_i/ask_qty_i for i in [0, levels). L2 depth
is what unlocks queue-position / maker-fill-probability modeling in Phase 2B.

Writes are ATOMIC per-flush shards: each flush writes a new file via a temp +
rename (never rewrites a growing daily file), so a concurrent reader never
sees a partial write and disk churn is O(rows) not O(n^2). A crash loses at
most the un-flushed batch. Bounded-backoff reconnection.
"""

from __future__ import annotations

import argparse
import asyncio
import heapq
import logging
import math
import os
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from vnedge.data.candles import (
    Candle,
    CandleParquetStore,
    CandlePipeline,
    Trade,
    floor_time,
)
from vnedge.data.lake_health import LakeHealthMonitor

logger = logging.getLogger(__name__)

FLUSH_EVERY = 500       # records
FLUSH_SECONDS = 30.0
_BACKOFF = 2.0
_SEEN_TRADE_IDS = 50_000
_SKIP_LOG_SECONDS = 60.0
# Public trade websocket batches can cross in flight. Hold the newest quarter
# second so a slightly late predecessor is ordered before the canonical candle
# builder sees it. This is bounded event-time ordering, not a data rewrite.
_TRADE_REORDER_MS = 250
_DELTA_NATIVE_IDS = {"delta_india", "delta", "deltaindia"}


def _canonical_symbol(symbol: str) -> str:
    """Match the tick-lake/candle-store symbol key used by the dashboard."""
    return symbol.split(":", 1)[0].replace("/", "")


def _normalize_trade_batch(
    trades: list[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], str | None]], int]:
    """Validate and stably order one CCXT trade update.

    Public websocket caches can include malformed placeholders and are not
    guaranteed to arrive in timestamp order. Invalid rows are measurement
    gaps, not a reason to abort every valid trade in the same update.
    """
    accepted: list[tuple[dict[str, Any], str | None]] = []
    rejected = 0
    for trade in trades:
        try:
            timestamp = int(trade["timestamp"])
            price = float(trade["price"])
            amount = float(trade["amount"])
        except (KeyError, TypeError, ValueError, OverflowError):
            rejected += 1
            continue
        if timestamp <= 0 or not math.isfinite(price) or price <= 0:
            rejected += 1
            continue
        if not math.isfinite(amount) or amount <= 0:
            rejected += 1
            continue
        trade_id = trade.get("id")
        key = f"{timestamp}:{trade_id}" if trade_id not in (None, "") else None
        accepted.append(
            (
                {
                    "ts_ms": timestamp,
                    "price": price,
                    "amount": amount,
                    "side": str(trade.get("side") or ""),
                    "trade_id": str(trade_id) if trade_id not in (None, "") else None,
                },
                key,
            )
        )
    accepted.sort(key=lambda item: item[0]["ts_ms"])
    return accepted, rejected


class CanonicalCandleSink:
    """Feed public trades into per-symbol canonical candle pipelines.

    This is measurement-only plumbing. It consumes the same public trades that
    are durably written to the tick lake and persists only closed candles.
    """

    def __init__(
        self,
        exchange: str,
        symbols: list[str],
        root: Path | str,
        *,
        tick_root: Path | str | None = None,
        restore_at: datetime | None = None,
        subscribers: Iterable[Callable[[Candle], None]] = (),
    ) -> None:
        store = CandleParquetStore(root, exchange=exchange)
        self.exchange = exchange
        self.symbols = tuple(symbols)
        bound_subscribers = tuple(subscribers)
        self.pipelines = {
            symbol: CandlePipeline(
                _canonical_symbol(symbol),
                store=store,
                subscribers=bound_subscribers,
            )
            for symbol in symbols
        }
        self.restored_last_trade_ts_ms: dict[str, int] = {}
        self.restored_trade_keys: dict[str, set[str]] = {
            symbol: set() for symbol in symbols
        }
        if tick_root is not None:
            self.restore_forming_from_tick_lake(
                Path(tick_root), at=restore_at or datetime.now(UTC)
            )

    @staticmethod
    def _candidate_shards(
        tick_root: Path,
        exchange: str,
        symbol: str,
        replay_start_ms: int,
        replay_through_ms: int,
    ) -> list[Path]:
        start_day = datetime.fromtimestamp(replay_start_ms / 1000, tz=UTC).date()
        end_day = datetime.fromtimestamp(replay_through_ms / 1000, tz=UTC).date()
        # A timed flush can straddle the replay boundary. Include shards that
        # began in the prior minute, then filter rows precisely below. The
        # recorder flushes at least every 30 seconds, so this remains bounded.
        cutoff = replay_start_ms - 60_000
        selected: list[Path] = []
        day = start_day
        while day <= end_day:
            shard_dir = (
                tick_root
                / "ticks"
                / f"exchange={exchange}"
                / f"symbol={_canonical_symbol(symbol)}"
                / "stream=trades"
                / day.strftime("%Y%m%d")
            )
            if shard_dir.exists():
                for path in shard_dir.glob("*.parquet"):
                    try:
                        first_ts = int(path.stem.split("-", 1)[0])
                    except (ValueError, IndexError):
                        continue
                    if cutoff <= first_ts <= replay_through_ms:
                        selected.append(path)
            day += timedelta(days=1)
        return sorted(selected)

    def restore_forming_from_tick_lake(self, tick_root: Path, *, at: datetime) -> int:
        """Rebuild the uncommitted tail from durable trade shards.

        Deployments commonly restart inside a minute. The old recorder began
        with an empty builder, permanently dropping both the pre-restart part
        of the current minute and, when shutdown crossed a boundary, the last
        fully observed minute. That one missing 1m child suppresses its exact
        5m/15m/1h parents and leaves scanner lanes timing out.

        Each pipeline restores its immutable ``closed_through`` boundary from
        Parquet. Replay starts exactly there and runs through ``at``: already
        committed candles are never rewritten, a durable missed minute is
        closed normally, and the current minute remains forming. This is the
        delta-only restart path; historical repair remains the recovery
        worker's responsibility.
        """
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("restore_at must be timezone-aware")
        bucket_open = floor_time(at.astimezone(UTC), "1m")
        through_ms = int(at.timestamp() * 1000)
        restored = 0
        for symbol in self.symbols:
            pipeline = self.pipelines[symbol]
            replay_start = pipeline.builder.closed_through or bucket_open
            replay_start_ms = int(replay_start.timestamp() * 1000)
            shards = self._candidate_shards(
                tick_root,
                self.exchange,
                symbol,
                replay_start_ms,
                through_ms,
            )
            if not shards:
                continue
            frames: list[pd.DataFrame] = []
            for path in shards:
                try:
                    frames.append(pd.read_parquet(path))
                except Exception:
                    logger.exception("failed to read forming recovery shard %s", path)
            if not frames:
                continue
            frame = pd.concat(frames, ignore_index=True)
            required = {"ts_ms", "price", "amount"}
            if not required.issubset(frame.columns):
                logger.error("forming recovery skipped: trade shard schema incomplete for %s", symbol)
                continue
            ts = pd.to_numeric(frame["ts_ms"], errors="coerce")
            frame = frame.loc[
                (ts >= replay_start_ms) & (ts <= through_ms)
            ].copy()
            if frame.empty:
                continue
            frame["ts_ms"] = pd.to_numeric(frame["ts_ms"], errors="raise").astype("int64")
            if "trade_id" in frame.columns:
                ids = frame["trade_id"].astype("string")
                with_id = ids.notna() & (ids.str.len() > 0)
                frame = pd.concat(
                    [
                        frame.loc[~with_id],
                        frame.loc[with_id].drop_duplicates("trade_id", keep="last"),
                    ],
                    ignore_index=True,
                )
            frame = frame.sort_values("ts_ms", kind="stable")
            trades: list[Trade] = []
            keys: set[str] = set()
            for row in frame.to_dict("records"):
                side = str(row.get("side") or "").lower()
                trade_id = row.get("trade_id")
                if trade_id is not None and not pd.isna(trade_id) and str(trade_id):
                    keys.add(f"{int(row['ts_ms'])}:{trade_id}")
                trades.append(
                    Trade(
                        timestamp=datetime.fromtimestamp(int(row["ts_ms"]) / 1000, tz=UTC),
                        price=Decimal(str(row["price"])),
                        amount=Decimal(str(row["amount"])),
                        is_buyer_maker=(
                            False if side == "buy" else True if side == "sell" else None
                        ),
                    )
                )
            for trade in trades:
                pipeline.on_trade(
                    trade.timestamp,
                    trade.price,
                    trade.amount,
                    trade.is_buyer_maker,
                )
            self.restored_last_trade_ts_ms[symbol] = int(frame["ts_ms"].max())
            self.restored_trade_keys[symbol] = keys
            restored += len(trades)
            logger.info(
                "restored canonical tail from tick lake: exchange=%s symbol=%s "
                "from=%s forming_bucket=%s trades=%d",
                self.exchange,
                symbol,
                replay_start.isoformat(),
                bucket_open.isoformat(),
                len(trades),
            )
        return restored

    def on_trade(self, symbol: str, trade: dict[str, Any]) -> None:
        side = str(trade.get("side") or "").lower()
        buyer_maker = False if side == "buy" else True if side == "sell" else None
        self.pipelines[symbol].on_trade(
            datetime.fromtimestamp(int(trade["timestamp"]) / 1000, tz=UTC),
            trade["price"],
            trade["amount"],
            buyer_maker,
        )

    def advance_time(self, now: datetime) -> None:
        for pipeline in self.pipelines.values():
            pipeline.advance_time(now)


def _book_row(
    ob: dict,
    levels: int,
    ts_ms: int,
    *,
    received_ts_ms: int | None = None,
    sequence: int | str | None = None,
    source: str | None = None,
    exchange_timestamped: bool | None = None,
) -> dict:
    """Flatten a CCXT order book into one L2 row: level-0 L1 aliases
    (bid/bid_qty/ask/ask_qty, kept for the top-of-book replay engine) plus the
    bid_px_i/bid_qty_i/ask_px_i/ask_qty_i ladder for i in [0, levels). Missing
    levels are padded with NaN price / 0.0 qty so the schema is fixed-width."""
    bids, asks = ob["bids"], ob["asks"]
    row: dict[str, Any] = {
        "ts_ms": ts_ms,
        "bid": float(bids[0][0]), "bid_qty": float(bids[0][1]),
        "ask": float(asks[0][0]), "ask_qty": float(asks[0][1]),
    }
    if received_ts_ms is not None:
        row["received_ts_ms"] = int(received_ts_ms)
    if sequence is not None and not isinstance(sequence, bool):
        row["sequence"] = sequence
    if source:
        row["source"] = source
    if exchange_timestamped is not None:
        row["exchange_timestamped"] = bool(exchange_timestamped)
    for i in range(levels):
        b = bids[i] if i < len(bids) else (float("nan"), 0.0)
        a = asks[i] if i < len(asks) else (float("nan"), 0.0)
        row[f"bid_px_{i}"] = float(b[0])
        row[f"bid_qty_{i}"] = float(b[1])
        row[f"ask_px_{i}"] = float(a[0])
        row[f"ask_qty_{i}"] = float(a[1])
    return row


class _Buffer:
    """Accumulates rows and writes atomic per-flush shard files for one stream.

    Each flush writes a NEW shard under stream=<s>/<day>/ via temp + atomic
    rename — never rewriting a growing file — so readers never catch a partial
    write and disk cost stays O(rows). Shard names sort by first-row time."""

    def __init__(self, root: Path, exchange: str, symbol: str, stream: str) -> None:
        self.root = root
        self.exchange = exchange
        self.symbol = symbol
        self.stream = stream
        self._rows: list[dict] = []
        self._last_flush = 0.0
        self._seq = 0

    def _shard_dir(self, day: str) -> Path:
        safe = self.symbol.split(":")[0].replace("/", "")
        d = (self.root / "ticks" / f"exchange={self.exchange}"
             / f"symbol={safe}" / f"stream={self.stream}" / day)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def add(self, row: dict) -> None:
        self._rows.append(row)

    def should_flush(self, now: float) -> bool:
        return len(self._rows) >= FLUSH_EVERY or (
            bool(self._rows) and now - self._last_flush >= FLUSH_SECONDS
        )

    def flush(self, now: float) -> int:
        if not self._rows:
            return 0
        df = pd.DataFrame(self._rows)
        n = len(df)
        # group by UTC day so a batch spanning midnight splits correctly
        df["_day"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True).dt.strftime("%Y%m%d")
        for day, chunk in df.groupby("_day"):
            chunk = chunk.drop(columns="_day")
            d = self._shard_dir(day)
            first_ts = int(chunk["ts_ms"].iloc[0])
            name = f"{first_ts}-{self._seq:06d}.parquet"
            final = d / name
            tmp = d / f".{name}.tmp"
            chunk.to_parquet(tmp, index=False)
            os.replace(tmp, final)   # atomic publish; readers only see complete shards
        self._seq += 1
        self._rows.clear()
        self._last_flush = now
        return n


class TickRecorder:
    def __init__(self, exchange_id: str, symbols: list[str], root: Path,
                 *, levels: int = 10, candle_root: Path | None = None,
                 trades_only: bool = False, books_only: bool = False) -> None:
        import ccxt.pro as ccxtpro

        if not hasattr(ccxtpro, exchange_id):
            raise ValueError(f"unknown CCXT Pro exchange id: {exchange_id}")
        if levels < 1:
            raise ValueError("levels must be >= 1")
        self._ex = getattr(ccxtpro, exchange_id)(
            {"enableRateLimit": True, "newUpdates": True}
        )
        self.exchange_id = exchange_id
        self.symbols = symbols
        self.root = root
        self.levels = levels
        # depth-stream limit BOTH Binance USDT-M and Bybit swaps accept (Bybit
        # rejects 5/10/20 — only {1,50,200,1000}); we slice to `levels` on write.
        self._book_limit = 50 if levels <= 50 else 200
        if trades_only and books_only:
            raise ValueError("trades_only and books_only are mutually exclusive")
        self.trades_only = trades_only
        # A books-only recorder can run ALONGSIDE a trades-only one without the
        # two writing the same stream: without this, a second recorder added for
        # L2 also subscribes to trades and duplicate shards land in the same
        # partition, double-counting volume for anything that reads them.
        self.books_only = books_only
        # Lake health is checked on a cycle, not assumed: an unchecked lake
        # reports UNKNOWN rather than healthy (2026-08-19 audit -- three
        # symbols silently lost an hour while the cockpit said "ok").
        self.lake_health = (
            LakeHealthMonitor(
                exchange=exchange_id,
                symbols=[_canonical_symbol(sym) for sym in symbols],
                candle_root=Path(candle_root),
                gap_root=Path(candle_root).parent / "gaps",
            )
            if candle_root is not None
            else None
        )
        self.candle_sink = (
            CanonicalCandleSink(
                exchange_id,
                symbols,
                candle_root,
                tick_root=root,
            )
            if candle_root is not None
            else None
        )
        self.trade_count = 0
        self.book_count = 0
        self._last_trade_ts_ms: dict[str, int] = {}
        self._seen_trade_ids: dict[str, set[str]] = {
            symbol: set(
                self.candle_sink.restored_trade_keys.get(symbol, set())
                if self.candle_sink is not None
                else ()
            )
            for symbol in symbols
        }
        self._seen_trade_order: dict[str, deque[str]] = {
            symbol: deque(
                sorted(self._seen_trade_ids[symbol])[-_SEEN_TRADE_IDS:]
            )
            for symbol in symbols
        }
        self._skipped_trade_counts: dict[str, list[int]] = {
            symbol: [0, 0, 0, 0] for symbol in symbols
        }
        self._next_skip_log: dict[str, float] = {
            symbol: 0.0 for symbol in symbols
        }
        self._trade_bufs: dict[str, _Buffer] = {
            symbol: _Buffer(root, exchange_id, symbol, "trades")
            for symbol in symbols
        }
        self._trade_reorder: dict[
            str, list[tuple[int, int, dict[str, Any], str | None]]
        ] = {symbol: [] for symbol in symbols}
        self._pending_trade_ids: dict[str, set[str]] = {
            symbol: set() for symbol in symbols
        }
        self._max_seen_trade_ts_ms: dict[str, int] = {}
        self._trade_arrival_seq = 0
        if self.candle_sink is not None:
            self._last_trade_ts_ms.update(
                self.candle_sink.restored_last_trade_ts_ms
            )

    def _remember_trade(self, symbol: str, key: str | None) -> None:
        if key is None:
            return
        seen = self._seen_trade_ids[symbol]
        order = self._seen_trade_order[symbol]
        if len(order) >= _SEEN_TRADE_IDS:
            seen.discard(order.popleft())
        order.append(key)
        seen.add(key)

    def _report_skipped_trades(
        self,
        symbol: str,
        *,
        malformed: int,
        late: int,
        duplicate: int,
        candle_rejected: int,
    ) -> None:
        if not (malformed or late or candle_rejected):
            if duplicate:
                logger.debug("%s skipped %d replayed trade rows", symbol, duplicate)
            return
        totals = self._skipped_trade_counts[symbol]
        for index, count in enumerate(
            (malformed, late, duplicate, candle_rejected)
        ):
            totals[index] += count
        now = time.monotonic()
        if now < self._next_skip_log[symbol]:
            return
        logger.warning(
            "%s skipped trade rows (60s summary): malformed=%d late=%d "
            "duplicate=%d candle=%d",
            symbol,
            *totals,
        )
        totals[:] = [0, 0, 0, 0]
        self._next_skip_log[symbol] = now + _SKIP_LOG_SECONDS

    def _ingest_trade_batch(
        self,
        symbol: str,
        trades: list[dict[str, Any]],
        buf: _Buffer,
    ) -> None:
        candidates, malformed = _normalize_trade_batch(trades)
        late = 0
        duplicate = 0
        last_timestamp = self._last_trade_ts_ms.get(symbol)
        seen = self._seen_trade_ids[symbol]
        pending_ids = self._pending_trade_ids[symbol]
        pending = self._trade_reorder[symbol]
        for row, key in candidates:
            timestamp = int(row["ts_ms"])
            if last_timestamp is not None and timestamp < last_timestamp:
                late += 1
                continue
            if key is not None and (key in seen or key in pending_ids):
                duplicate += 1
                continue
            self._trade_arrival_seq += 1
            heapq.heappush(
                pending,
                (timestamp, self._trade_arrival_seq, row, key),
            )
            if key is not None:
                pending_ids.add(key)
            self._max_seen_trade_ts_ms[symbol] = max(
                timestamp,
                self._max_seen_trade_ts_ms.get(symbol, timestamp),
            )
        watermark = self._max_seen_trade_ts_ms.get(symbol)
        drained_late, candle_rejected = self._drain_trade_reorder(
            symbol,
            buf,
            through_ms=(watermark - _TRADE_REORDER_MS if watermark is not None else None),
        )
        late += drained_late
        self._report_skipped_trades(
            symbol,
            malformed=malformed,
            late=late,
            duplicate=duplicate,
            candle_rejected=candle_rejected,
        )

    def _drain_trade_reorder(
        self,
        symbol: str,
        buf: _Buffer,
        *,
        through_ms: int | None = None,
        force: bool = False,
    ) -> tuple[int, int]:
        """Publish event-time ordered trades through a bounded watermark."""
        pending = self._trade_reorder[symbol]
        pending_ids = self._pending_trade_ids[symbol]
        late = 0
        candle_rejected = 0
        last_timestamp = self._last_trade_ts_ms.get(symbol)
        while pending and (force or (through_ms is not None and pending[0][0] <= through_ms)):
            timestamp, _, row, key = heapq.heappop(pending)
            if key is not None:
                pending_ids.discard(key)
            if last_timestamp is not None and timestamp < last_timestamp:
                late += 1
                continue
            if self.candle_sink is not None:
                try:
                    self.candle_sink.on_trade(
                        symbol,
                        {
                            "timestamp": timestamp,
                            "price": row["price"],
                            "amount": row["amount"],
                            "side": row["side"],
                        },
                    )
                except ValueError as exc:
                    candle_rejected += 1
                    logger.warning(
                        "canonical trade skipped: symbol=%s timestamp=%s reason=%s",
                        symbol,
                        timestamp,
                        exc,
                    )
                    continue
            buf.add(row)
            self.trade_count += 1
            self._remember_trade(symbol, key)
            last_timestamp = timestamp
        if last_timestamp is not None:
            self._last_trade_ts_ms[symbol] = last_timestamp
        return late, candle_rejected

    async def _watch_trades(self, symbol: str, clock) -> None:
        buf = self._trade_bufs[symbol]
        while True:
            try:
                trades = await self._ex.watch_trades(symbol)
                self._ingest_trade_batch(symbol, trades, buf)
                now = clock()
                if buf.should_flush(now):
                    buf.flush(now)
            except asyncio.CancelledError:
                late, candle_rejected = self._drain_trade_reorder(
                    symbol, buf, force=True
                )
                self._report_skipped_trades(
                    symbol,
                    malformed=0,
                    late=late,
                    duplicate=0,
                    candle_rejected=candle_rejected,
                )
                buf.flush(clock())
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s trades error: %s", symbol, exc)
                await asyncio.sleep(_BACKOFF)

    async def _maintain_trade_streams(self, clock) -> None:
        """Flush quiet streams and close candles behind the reorder watermark."""
        while True:
            now = datetime.now(UTC)
            through_ms = int(now.timestamp() * 1000) - _TRADE_REORDER_MS
            for symbol, buf in self._trade_bufs.items():
                late, candle_rejected = self._drain_trade_reorder(
                    symbol, buf, through_ms=through_ms
                )
                self._report_skipped_trades(
                    symbol,
                    malformed=0,
                    late=late,
                    duplicate=0,
                    candle_rejected=candle_rejected,
                )
                if buf.should_flush(clock()):
                    buf.flush(clock())
            if self.candle_sink is not None:
                self.candle_sink.advance_time(
                    now - timedelta(milliseconds=_TRADE_REORDER_MS)
                )
            await asyncio.sleep(1.0)

    async def _watch_book(self, symbol: str, clock) -> None:
        buf = _Buffer(self.root, self.exchange_id, symbol, "book")
        while True:
            try:
                ob = await self._ex.watch_order_book(symbol, limit=self._book_limit)
                if ob["bids"] and ob["asks"]:
                    received_ts_ms = int(datetime.now(UTC).timestamp() * 1000)
                    exchange_timestamped = ob.get("timestamp") is not None
                    ts_ms = int(ob.get("timestamp") or received_ts_ms)
                    buf.add(
                        _book_row(
                            ob,
                            self.levels,
                            ts_ms,
                            received_ts_ms=received_ts_ms,
                            sequence=ob.get("nonce"),
                            source=f"{self.exchange_id}:watch_order_book",
                            exchange_timestamped=exchange_timestamped,
                        )
                    )
                    self.book_count += 1
                now = clock()
                if buf.should_flush(now):
                    buf.flush(now)
            except asyncio.CancelledError:
                buf.flush(clock())
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s book error: %s", symbol, exc)
                await asyncio.sleep(_BACKOFF)

    def streams_for(self, symbol: str) -> tuple[str, ...]:
        """Which streams this recorder owns for a symbol.

        Kept separate from ``run`` so the ownership rule is checkable without
        standing up an exchange connection -- two recorders writing the same
        stream is a data-corruption bug, not a runtime one.
        """
        streams = []
        if not self.books_only:
            streams.append("trades")
        if not self.trades_only:
            streams.append("book")
        return tuple(streams)

    async def run(self, clock=None) -> None:
        import time as _t

        clock = clock or _t.monotonic
        tasks = []
        for symbol in self.symbols:
            for stream in self.streams_for(symbol):
                watcher = (
                    self._watch_trades if stream == "trades" else self._watch_book
                )
                tasks.append(asyncio.create_task(watcher(symbol, clock)))
        if not self.books_only:
            tasks.append(asyncio.create_task(self._maintain_trade_streams(clock)))
        if self.lake_health is not None:
            tasks.append(asyncio.create_task(self.lake_health.run()))
        logger.info("tick recorder: %s %s -> %s", self.exchange_id, self.symbols, self.root)
        try:
            await asyncio.gather(*tasks)
        finally:
            await self._ex.close()


def _delta_ob(buy: list, sell: list) -> dict:
    """Convert Delta native l2_orderbook buy/sell arrays into a CCXT-shaped
    order book (bids descending, asks ascending) so ``_book_row`` can flatten
    it exactly like the CCXT Pro path. Delta entries are
    {"limit_price": <str>, "size": <num>, "depth": ...}."""
    return {
        "bids": [[float(e["limit_price"]), float(e["size"])] for e in buy],
        "asks": [[float(e["limit_price"]), float(e["size"])] for e in sell],
    }


class DeltaTickRecorder:
    """Records Delta India L2 books + trades to the same Parquet tick lake.

    Delta has no CCXT Pro class, so this drives the native
    ``DeltaPublicWsClient``: its ``on_book`` / ``on_trade`` callbacks fill the
    same ``_Buffer`` instances ``TickRecorder`` uses, and a flush loop persists
    them (parquet IO kept off the websocket reader path). Output lands under
    ``ticks/exchange=delta_india/…`` so the L2 research lake and scalper
    discovery pick it up with no other changes.
    """

    def __init__(
        self,
        symbols: list[str],
        root: Path,
        *,
        levels: int = 10,
        exchange_id: str = "delta_india",
        candle_root: Path | None = None,
        trades_only: bool = False,
        books_only: bool = False,
        url: str | None = None,
        connect=None,
        clock=None,
    ) -> None:
        from vnedge.exchange.delta_ws import (
            DELTA_INDIA_WS_URL,
            DeltaPublicWsClient,
            delta_native_symbol,
        )

        if levels < 1:
            raise ValueError("levels must be >= 1")
        if trades_only and books_only:
            raise ValueError("trades_only and books_only are mutually exclusive")
        root = Path(root)
        self.exchange_id = exchange_id
        self.symbols = [delta_native_symbol(s) for s in symbols]
        self.root = root
        self.levels = levels
        self.trades_only = trades_only
        self.books_only = books_only
        self._clock = clock
        self.candle_sink = (
            CanonicalCandleSink(
                exchange_id,
                self.symbols,
                candle_root,
                tick_root=root,
            )
            if candle_root is not None
            else None
        )
        self.trade_count = 0
        self.book_count = 0
        self._trade_bufs = {
            s: _Buffer(root, exchange_id, s, "trades") for s in self.symbols
        }
        self._book_bufs = {
            s: _Buffer(root, exchange_id, s, "book") for s in self.symbols
        }
        channels = tuple(
            channel
            for channel, enabled in (
                ("l2_orderbook", not trades_only),
                ("all_trades", not books_only),
            )
            if enabled
        )
        self._client = DeltaPublicWsClient(
            self.symbols,
            channels=channels,
            url=url or DELTA_INDIA_WS_URL,
            connect=connect,
            on_book=self._on_book,
            on_trade=self._on_trade,
        )

    @staticmethod
    def _epoch_ms() -> int:
        from datetime import UTC, datetime

        return int(datetime.now(UTC).timestamp() * 1000)

    def _on_book(self, sym: str, buy: list, sell: list, msg: dict) -> None:
        if not buy or not sell:
            return
        buf = self._book_bufs.get(sym)
        if buf is None:
            return
        ts_raw = msg.get("timestamp")
        ts_ms = int(ts_raw) // 1000 if ts_raw is not None else self._epoch_ms()
        try:
            buf.add(
                _book_row(
                    _delta_ob(buy, sell),
                    self.levels,
                    ts_ms,
                    received_ts_ms=self._epoch_ms(),
                    sequence=(
                        msg.get("sequence")
                        or msg.get("sequence_no")
                        or msg.get("nonce")
                    ),
                    source=f"{self.exchange_id}:l2_orderbook",
                    exchange_timestamped=ts_raw is not None,
                )
            )
        except (KeyError, TypeError, ValueError):
            return
        self.book_count += 1

    def _on_trade(self, sym: str, trade: dict) -> None:
        buf = self._trade_bufs.get(sym)
        if buf is None:
            return
        row = {
            "ts_ms": int(trade["ts_ms"]),
            "price": float(trade["price"]),
            "amount": float(trade["size"]),
            "side": trade.get("side", ""),
            "trade_id": trade.get("trade_id"),
        }
        if self.candle_sink is not None:
            self.candle_sink.on_trade(
                sym,
                {
                    "timestamp": row["ts_ms"],
                    "price": row["price"],
                    "amount": row["amount"],
                    "side": row["side"],
                },
            )
        buf.add(row)
        self.trade_count += 1

    def _all_buffers(self):
        return (*self._trade_bufs.values(), *self._book_bufs.values())

    def streams_for(self, symbol: str) -> tuple[str, ...]:
        """Which streams this recorder owns for a symbol.

        Kept separate from ``run`` so the ownership rule is checkable without
        standing up an exchange connection -- two recorders writing the same
        stream is a data-corruption bug, not a runtime one.
        """
        streams = []
        if not self.books_only:
            streams.append("trades")
        if not self.trades_only:
            streams.append("book")
        return tuple(streams)

    async def run(self, clock=None) -> None:
        import time as _t

        clock = clock or self._clock or _t.monotonic
        await self._client.start()
        logger.info(
            "delta tick recorder: %s %s -> %s",
            self.exchange_id, self.symbols, self.root,
        )
        try:
            while True:
                now = clock()
                for buf in self._all_buffers():
                    if buf.should_flush(now):
                        buf.flush(now)
                if self.candle_sink is not None:
                    self.candle_sink.advance_time(datetime.now(UTC))
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            now = clock()
            for buf in self._all_buffers():
                buf.flush(now)
            raise
        finally:
            await self._client.stop()


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="zero-risk tick/book recorder")
    p.add_argument("--exchange", default="binanceusdm")
    p.add_argument("--symbols", default="BTC/USDT:USDT")
    p.add_argument("--data-root", default="data")
    p.add_argument(
        "--candle-root",
        help="optional canonical candle store populated from the live trade stream",
    )
    p.add_argument(
        "--trades-only",
        action="store_true",
        help="record trades without the L2 book stream (sufficient for candles)",
    )
    p.add_argument(
        "--books-only",
        action="store_true",
        help="record the L2 book without the trade stream (use when a separate "
             "recorder already owns trades for these symbols)",
    )
    p.add_argument("--levels", type=int, default=10, help="L2 depth levels per side")
    args = p.parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.exchange in _DELTA_NATIVE_IDS:
        recorder: DeltaTickRecorder | TickRecorder = DeltaTickRecorder(
            symbols,
            Path(args.data_root),
            levels=args.levels,
            exchange_id=args.exchange,
            candle_root=Path(args.candle_root) if args.candle_root else None,
            trades_only=args.trades_only,
            books_only=args.books_only,
        )
    else:
        recorder = TickRecorder(
            args.exchange,
            symbols,
            Path(args.data_root),
            levels=args.levels,
            candle_root=Path(args.candle_root) if args.candle_root else None,
            trades_only=args.trades_only,
            books_only=args.books_only,
        )
    asyncio.run(recorder.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
