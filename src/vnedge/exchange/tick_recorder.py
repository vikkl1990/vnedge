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
import hashlib
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
from vnedge.data.market_records import PublicTrade
from vnedge.data.symbols import canonical_symbol
from vnedge.runtime.latency_store import RecorderLatencyStore
from vnedge.runtime.latency_tracker import TRADE_INGEST_MS, LatencyTracker

logger = logging.getLogger(__name__)

FLUSH_EVERY = 500  # records
FLUSH_SECONDS = 30.0
_BACKOFF = 2.0
_SEEN_TRADE_IDS = 50_000
_SKIP_LOG_SECONDS = 60.0
# Public trade websocket batches can cross in flight. Hold the newest quarter
# second so a slightly late predecessor is ordered before the canonical candle
# builder sees it. This is bounded event-time ordering, not a data rewrite.
_TRADE_REORDER_MS = 250
_TRADE_FUTURE_SLACK_MS = 5_000
_DELTA_NATIVE_IDS = {"delta_india", "delta", "deltaindia"}


def _normalize_trade_batch(
    trades: list[dict[str, Any]],
    *,
    received_at_ms: int | None = None,
) -> tuple[list[tuple[dict[str, Any], str | None]], int]:
    """Validate and stably order one CCXT trade update.

    Public websocket caches can include malformed placeholders and are not
    guaranteed to arrive in timestamp order. Invalid rows are measurement
    gaps, not a reason to abort every valid trade in the same update.
    """
    accepted: list[tuple[dict[str, Any], str | None]] = []
    rejected = 0
    now_ms = received_at_ms or int(datetime.now(UTC).timestamp() * 1000)
    for trade in trades:
        try:
            timestamp = int(trade["timestamp"])
            price = float(trade["price"])
            amount = float(trade["amount"])
        except (KeyError, TypeError, ValueError, OverflowError):
            rejected += 1
            continue
        if (
            timestamp <= 0
            or timestamp > now_ms + _TRADE_FUTURE_SLACK_MS
            or not math.isfinite(price)
            or price <= 0
        ):
            rejected += 1
            continue
        if not math.isfinite(amount) or amount <= 0:
            rejected += 1
            continue
        trade_id = trade.get("id")
        if trade_id in (None, ""):
            rejected += 1
            continue
        key = f"{timestamp}:{trade_id}"
        accepted.append(
            (
                {
                    "ts_ms": timestamp,
                    "price": price,
                    "amount": amount,
                    "side": str(trade.get("side") or ""),
                    "trade_id": str(trade_id),
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
        timing_sink: Callable[[str, float], None] | None = None,
    ) -> None:
        store = CandleParquetStore(root, exchange=exchange)
        self.exchange = exchange
        self.symbols = tuple(symbols)
        bound_subscribers = tuple(subscribers)
        self.pipelines = {
            symbol: CandlePipeline(
                canonical_symbol(symbol),
                store=store,
                subscribers=bound_subscribers,
                timing_sink=timing_sink,
            )
            for symbol in symbols
        }
        self.restored_last_trade_ts_ms: dict[str, int] = {}
        self.restored_trade_keys: dict[str, set[str]] = {symbol: set() for symbol in symbols}
        if tick_root is not None:
            self.restore_forming_from_tick_lake(Path(tick_root), at=restore_at or datetime.now(UTC))

    def would_publish_on_trade(self, symbol: str, timestamp_ms: int) -> bool:
        """Whether this trade will close the current base candle.

        The recorder uses this boundary to make the raw-trade shard durable
        before the candle can be published to an in-memory subscriber.
        """
        pipeline = self.pipelines[symbol]
        forming = pipeline.builder.forming()
        if forming is None:
            return False
        timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        return timestamp >= forming.close_time

    def would_publish_on_advance(self, now: datetime) -> bool:
        """Return whether wall-clock advancement can close any base candle."""
        return any(
            (forming := pipeline.builder.forming()) is not None and now >= forming.close_time
            for pipeline in self.pipelines.values()
        )

    def new_arm_block_reason(self, symbol: str) -> str | None:
        """Return why a colocated lane must not open new risk.

        Raw trades are flushed synchronously before a boundary candle can be
        published.  The remaining publish-before-upsert durability window is
        represented by ``CandlePipeline.persistence_healthy``.  Exits do not
        call this probe; it exists only for the lane new-arm gate.
        """
        wanted = canonical_symbol(symbol)
        for venue_symbol, pipeline in self.pipelines.items():
            if canonical_symbol(venue_symbol) != wanted:
                continue
            if not pipeline.persistence_healthy:
                return "canonical_persist_unhealthy"
            return None
        return "canonical_producer_symbol_unowned"

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
                / f"symbol={canonical_symbol(symbol)}"
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
                logger.error(
                    "forming recovery skipped: trade shard schema incomplete for %s", symbol
                )
                continue
            ts = pd.to_numeric(frame["ts_ms"], errors="coerce")
            frame = frame.loc[(ts >= replay_start_ms) & (ts <= through_ms)].copy()
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
        for symbol, pipeline in self.pipelines.items():
            try:
                pipeline.advance_time(now)
            except Exception:
                logger.exception(
                    "canonical pipeline advance failed; symbol isolated: %s",
                    symbol,
                )


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
    if not bids or not asks:
        raise ValueError("book must contain at least one bid and ask")
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    if not math.isfinite(best_bid) or best_bid <= 0:
        raise ValueError("book bid must be finite and positive")
    if not math.isfinite(best_ask) or best_ask <= 0:
        raise ValueError("book ask must be finite and positive")
    if best_ask < best_bid:
        raise ValueError("book ask must not be below bid")
    row: dict[str, Any] = {
        "ts_ms": ts_ms,
        "bid": best_bid,
        "bid_qty": float(bids[0][1]),
        "ask": best_ask,
        "ask_qty": float(asks[0][1]),
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
        safe = canonical_symbol(self.symbol)
        d = (
            self.root
            / "ticks"
            / f"exchange={self.exchange}"
            / f"symbol={safe}"
            / f"stream={self.stream}"
            / day
        )
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
            # Atomic rename protects concurrent readers; fsync protects the
            # rebuild tape across a host crash. A boundary trade cannot reach
            # CandlePipeline until this method returns.
            with tmp.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(tmp, final)  # atomic publish; readers only see complete shards
            directory_fd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        self._seq += 1
        self._rows.clear()
        self._last_flush = now
        return n


class TickRecorder:
    def __init__(
        self,
        exchange_id: str,
        symbols: list[str],
        root: Path,
        *,
        levels: int = 10,
        candle_root: Path | None = None,
        trades_only: bool = False,
        books_only: bool = False,
        candle_subscribers: Iterable[Callable[[Candle], None]] = (),
    ) -> None:
        import ccxt.pro as ccxtpro

        root = Path(root)

        if not hasattr(ccxtpro, exchange_id):
            raise ValueError(f"unknown CCXT Pro exchange id: {exchange_id}")
        if levels < 1:
            raise ValueError("levels must be >= 1")
        self._ex = getattr(ccxtpro, exchange_id)({"enableRateLimit": True, "newUpdates": True})
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
        self.recorder_latency = LatencyTracker()
        self.recorder_latency_store = RecorderLatencyStore(
            self.root / "reports" / "recorder_latency" / f"{exchange_id}.json",
            process_id=f"pulse-recorder:{exchange_id}",
        )
        # Lake health is checked on a cycle, not assumed: an unchecked lake
        # reports UNKNOWN rather than healthy (2026-08-19 audit -- three
        # symbols silently lost an hour while the cockpit said "ok").
        self.lake_health = (
            LakeHealthMonitor(
                exchange=exchange_id,
                symbols=[canonical_symbol(sym) for sym in symbols],
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
                subscribers=candle_subscribers,
                timing_sink=self.recorder_latency.record,
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
            symbol: deque(sorted(self._seen_trade_ids[symbol])[-_SEEN_TRADE_IDS:])
            for symbol in symbols
        }
        self._skipped_trade_counts: dict[str, list[int]] = {
            symbol: [0, 0, 0, 0] for symbol in symbols
        }
        self._next_skip_log: dict[str, float] = {symbol: 0.0 for symbol in symbols}
        self._trade_bufs: dict[str, _Buffer] = {
            symbol: _Buffer(root, exchange_id, symbol, "trades") for symbol in symbols
        }
        self._trade_reorder: dict[str, list[tuple[int, int, dict[str, Any], str | None]]] = {
            symbol: [] for symbol in symbols
        }
        self._pending_trade_ids: dict[str, set[str]] = {symbol: set() for symbol in symbols}
        self._max_seen_trade_ts_ms: dict[str, int] = {}
        self._trade_arrival_seq = 0
        if self.candle_sink is not None:
            self._last_trade_ts_ms.update(self.candle_sink.restored_last_trade_ts_ms)

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
        for index, count in enumerate((malformed, late, duplicate, candle_rejected)):
            totals[index] += count
        now = time.monotonic()
        if now < self._next_skip_log[symbol]:
            return
        logger.warning(
            "%s skipped trade rows (60s summary): malformed=%d late=%d duplicate=%d candle=%d",
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
            latency = getattr(self, "recorder_latency", None)
            if latency is not None:
                received_ms = int(datetime.now(UTC).timestamp() * 1000)
                latency.record(TRADE_INGEST_MS, max(0.0, float(received_ms - timestamp)))
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
            side = str(row.get("side") or "").lower()
            public_trade = PublicTrade(
                exchange=getattr(self, "exchange_id", buf.exchange),
                symbol=symbol,
                trade_id=str(row["trade_id"]),
                timestamp=datetime.fromtimestamp(timestamp / 1000, tz=UTC),
                price=row["price"],
                amount=row["amount"],
                is_buyer_maker=(
                    False if side == "buy" else True if side == "sell" else None
                ),
            )
            public_trade.validate_clock(datetime.now(UTC))
            durable_row = public_trade.storage_row()
            # The raw row is the rebuild tape. Add it first and atomically
            # publish the shard before a boundary trade is allowed to emit the
            # prior closed candle. Forming-candle trades remain batched.
            buf.add(durable_row)
            needs_durable_flush = bool(
                self.candle_sink is not None
                and callable(getattr(self.candle_sink, "would_publish_on_trade", None))
                and self.candle_sink.would_publish_on_trade(symbol, timestamp)
            )
            if needs_durable_flush:
                buf.flush(time.monotonic())
            if self.candle_sink is not None:
                try:
                    self.candle_sink.on_trade(
                        symbol,
                        {
                            "timestamp": timestamp,
                            "price": public_trade.price,
                            "amount": public_trade.amount,
                            "side": durable_row["side"],
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
                late, candle_rejected = self._drain_trade_reorder(symbol, buf, force=True)
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
                boundary = now - timedelta(milliseconds=_TRADE_REORDER_MS)
                if self.candle_sink.would_publish_on_advance(boundary):
                    # Quiet-minute closure has no boundary trade to force the
                    # shard. Flush every symbol first: only then may any
                    # pipeline publish its closed candle.
                    for trade_buf in self._trade_bufs.values():
                        trade_buf.flush(clock())
                self.candle_sink.advance_time(boundary)
            await asyncio.sleep(1.0)

    async def _maintain_latency_snapshot(self) -> None:
        while True:
            self.recorder_latency_store.save_from(self.recorder_latency)
            await asyncio.sleep(5.0)

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

    def new_arm_block_reason(self, symbol: str) -> str | None:
        """Expose integrated-producer durability to scanner arm gates."""
        if self.candle_sink is None or self.books_only:
            return "canonical_producer_unavailable"
        return self.candle_sink.new_arm_block_reason(symbol)

    async def run(self, clock=None, *, acquire_writer_lease: bool = True) -> None:
        import time as _t

        from vnedge.exchange.writer_lease import CanonicalWriterLease

        clock = clock or _t.monotonic
        lease = (
            CanonicalWriterLease(self.root, self.exchange_id).acquire()
            if acquire_writer_lease
            else None
        )
        tasks = []
        for symbol in self.symbols:
            for stream in self.streams_for(symbol):
                watcher = self._watch_trades if stream == "trades" else self._watch_book
                tasks.append(asyncio.create_task(watcher(symbol, clock)))
        if not self.books_only:
            tasks.append(asyncio.create_task(self._maintain_trade_streams(clock)))
        tasks.append(asyncio.create_task(self._maintain_latency_snapshot()))
        if self.lake_health is not None:
            tasks.append(asyncio.create_task(self.lake_health.run()))
        logger.info("tick recorder: %s %s -> %s", self.exchange_id, self.symbols, self.root)
        try:
            await asyncio.gather(*tasks)
        finally:
            try:
                self.recorder_latency_store.save_from(self.recorder_latency)
                await self._ex.close()
            finally:
                if lease is not None:
                    lease.release()


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
        candle_subscribers: Iterable[Callable[[Candle], None]] = (),
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
        self.recorder_latency = LatencyTracker()
        self.recorder_latency_store = RecorderLatencyStore(
            root / "reports" / "recorder_latency" / f"{exchange_id}.json",
            process_id=f"pulse-recorder:{exchange_id}",
        )
        self._clock = clock
        self.candle_sink = (
            CanonicalCandleSink(
                exchange_id,
                self.symbols,
                candle_root,
                tick_root=root,
                subscribers=candle_subscribers,
                timing_sink=self.recorder_latency.record,
            )
            if candle_root is not None
            else None
        )
        self.trade_count = 0
        self.book_count = 0
        self._seen_trade_ids: set[str] = set()
        self._seen_trade_order: deque[str] = deque()
        self._trade_bufs = {s: _Buffer(root, exchange_id, s, "trades") for s in self.symbols}
        self._book_bufs = {s: _Buffer(root, exchange_id, s, "book") for s in self.symbols}
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
                    sequence=(msg.get("sequence") or msg.get("sequence_no") or msg.get("nonce")),
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
        trade_id = trade.get("trade_id")
        if trade_id in (None, ""):
            # Delta's public ``all_trades`` payload currently omits a native
            # trade id. Keep the raw-tape identity mandatory by deriving a
            # stable, explicitly-labelled fallback from every available event
            # identity field. Exact websocket replays then deduplicate; two
            # indistinguishable prints cannot honestly be separated without a
            # venue sequence and remain one measurement atom.
            identity = "|".join(
                str(trade.get(field) or "")
                for field in (
                    "ts_ms",
                    "price",
                    "size",
                    "buyer_role",
                    "seller_role",
                    "sequence",
                )
            )
            digest = hashlib.sha256(f"{sym}|{identity}".encode()).hexdigest()[:24]
            trade_id = f"delta-synthetic:{digest}"
        trade_id = str(trade_id)
        if trade_id in self._seen_trade_ids:
            return
        side = str(trade.get("side") or "").lower()
        try:
            public_trade = PublicTrade(
                exchange=self.exchange_id,
                symbol=sym,
                trade_id=str(trade_id),
                timestamp=datetime.fromtimestamp(int(trade["ts_ms"]) / 1000, tz=UTC),
                price=trade["price"],
                amount=trade["size"],
                is_buyer_maker=(
                    False if side == "buy" else True if side == "sell" else None
                ),
            )
            public_trade.validate_clock(datetime.now(UTC))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            logger.warning("%s trade skipped: %s", sym, exc)
            return
        row = public_trade.storage_row()
        if len(self._seen_trade_order) >= _SEEN_TRADE_IDS:
            self._seen_trade_ids.discard(self._seen_trade_order.popleft())
        self._seen_trade_ids.add(trade_id)
        self._seen_trade_order.append(trade_id)
        self.recorder_latency.record(
            TRADE_INGEST_MS,
            max(0.0, float(self._epoch_ms() - int(row["ts_ms"]))),
        )
        # Match the CCXT recorder's durability contract: the raw trade is the
        # rebuild tape and must be visible before a boundary trade can publish
        # the prior closed candle to an in-memory subscriber.
        buf.add(row)
        if self.candle_sink is not None and self.candle_sink.would_publish_on_trade(
            sym, int(row["ts_ms"])
        ):
            buf.flush((self._clock or time.monotonic)())
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

    def new_arm_block_reason(self, symbol: str) -> str | None:
        """Expose integrated-producer durability to scanner arm gates."""
        if self.candle_sink is None or self.books_only:
            return "canonical_producer_unavailable"
        return self.candle_sink.new_arm_block_reason(symbol)

    async def run(self, clock=None, *, acquire_writer_lease: bool = True) -> None:
        import time as _t

        from vnedge.exchange.writer_lease import CanonicalWriterLease

        clock = clock or self._clock or _t.monotonic
        lease = (
            CanonicalWriterLease(self.root, self.exchange_id).acquire()
            if acquire_writer_lease
            else None
        )
        try:
            await self._client.start()
            logger.info(
                "delta tick recorder: %s %s -> %s",
                self.exchange_id,
                self.symbols,
                self.root,
            )
            next_latency_snapshot = 0.0
            while True:
                now = clock()
                for buf in self._all_buffers():
                    if buf.should_flush(now):
                        buf.flush(now)
                if self.candle_sink is not None and self.candle_sink.would_publish_on_advance(
                    datetime.now(UTC)
                ):
                    for trade_buf in self._trade_bufs.values():
                        trade_buf.flush(now)
                if self.candle_sink is not None:
                    self.candle_sink.advance_time(datetime.now(UTC))
                if now >= next_latency_snapshot:
                    self.recorder_latency_store.save_from(self.recorder_latency)
                    next_latency_snapshot = now + 5.0
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            now = clock()
            for buf in self._all_buffers():
                buf.flush(now)
            raise
        finally:
            try:
                self.recorder_latency_store.save_from(self.recorder_latency)
                await self._client.stop()
            finally:
                if lease is not None:
                    lease.release()


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
