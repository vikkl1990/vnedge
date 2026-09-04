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
import json
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

TradeBody = tuple[int, float, float, str]

_TRADE_COUNTER_NAMES = (
    "trades_in",
    "trades_dup_ws",
    "trades_dup_pending",
    "trades_dup_replay",
    "trades_no_id",
    "trades_late_closed",
    "trades_conflict",
)


def _empty_trade_metrics() -> dict[str, int]:
    return {name: 0 for name in _TRADE_COUNTER_NAMES}


def _append_trade_conflict(
    root: Path,
    *,
    exchange: str,
    symbol: str,
    trade_id: str,
    previous: TradeBody,
    incoming: TradeBody,
    layer: str,
) -> None:
    """Durably expose a venue-ID collision before failing closed."""
    path = root / "reports" / "trade_integrity" / "conflicts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "observed_at": datetime.now(UTC).isoformat(),
        "exchange": exchange,
        "symbol": canonical_symbol(symbol),
        "trade_id": trade_id,
        "previous": list(previous),
        "incoming": list(incoming),
        "layer": layer,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_trade_metrics(
    root: Path,
    *,
    exchange: str,
    metrics: dict[str, dict[str, int | float]],
) -> None:
    path = root / "reports" / "trade_integrity" / f"{exchange}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "exchange": exchange,
        "symbols": metrics,
    }
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _trade_id(row: dict[str, Any]) -> str | None:
    value = row.get("trade_id")
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    normalized = str(value).strip()
    return normalized or None


def _trade_body(row: dict[str, Any]) -> TradeBody:
    return (
        int(row["ts_ms"]),
        float(row["price"]),
        float(row["amount"]),
        str(row.get("side") or "").lower(),
    )


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
        raw_trade_id = trade.get("id")
        trade_id = None if raw_trade_id is None else str(raw_trade_id).strip() or None
        key = f"{timestamp}:{trade_id}" if trade_id is not None else None
        accepted.append(
            (
                {
                    "ts_ms": timestamp,
                    "price": price,
                    "amount": amount,
                    "side": str(trade.get("side") or ""),
                    "trade_id": trade_id,
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
        self.restored_trade_bodies: dict[str, dict[str, TradeBody]] = {
            symbol: {} for symbol in symbols
        }
        self.restored_trade_metrics: dict[str, dict[str, int]] = {
            symbol: _empty_trade_metrics() for symbol in symbols
        }
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

    @staticmethod
    def _dedupe_replay_frame(
        frame: pd.DataFrame,
        *,
        symbol: str,
        conflict_root: Path | None = None,
        exchange: str = "unknown",
    ) -> tuple[pd.DataFrame, set[str], dict[str, TradeBody], int]:
        """Deduplicate identified rows and reject reused IDs with new bodies.

        Rows without a venue id are intentionally retained. Identical repeats
        keep the later durable copy; a changed timestamp/price/amount/side is
        a corrupt overlap and must not be silently selected.
        """
        no_id: list[tuple[int, dict[str, Any]]] = []
        by_id: dict[str, tuple[int, dict[str, Any]]] = {}
        bodies: dict[str, TradeBody] = {}
        duplicates = 0
        for row_index, row in enumerate(frame.to_dict("records")):
            trade_id = _trade_id(row)
            if trade_id is None:
                no_id.append((row_index, row))
                continue
            body = _trade_body(row)
            previous = bodies.get(trade_id)
            if previous is not None and previous != body:
                if conflict_root is not None:
                    _append_trade_conflict(
                        conflict_root,
                        exchange=exchange,
                        symbol=symbol,
                        trade_id=trade_id,
                        previous=previous,
                        incoming=body,
                        layer="restart_replay",
                    )
                raise ValueError(
                    f"conflicting durable trade body: symbol={canonical_symbol(symbol)} "
                    f"trade_id={trade_id!r} previous={previous!r} incoming={body!r}"
                )
            if previous is not None:
                duplicates += 1
            bodies[trade_id] = body
            by_id[trade_id] = (row_index, row)
        selected = [*no_id, *by_id.values()]
        selected.sort(key=lambda item: (int(item[1]["ts_ms"]), item[0]))
        deduped = pd.DataFrame([row for _index, row in selected], columns=frame.columns)
        keys = {f"{body[0]}:{trade_id}" for trade_id, body in bodies.items()}
        return deduped, keys, bodies, duplicates

    @staticmethod
    def _recent_trade_frame(
        tick_root: Path,
        exchange: str,
        symbol: str,
        *,
        through_ms: int,
        limit: int = _SEEN_TRADE_IDS,
    ) -> pd.DataFrame:
        """Read a bounded newest-first tail used only to restore dedup state."""
        stream = (
            tick_root
            / "ticks"
            / f"exchange={exchange}"
            / f"symbol={canonical_symbol(symbol)}"
            / "stream=trades"
        )
        frames: list[pd.DataFrame] = []
        rows = 0
        if not stream.exists():
            return pd.DataFrame()
        shards: list[Path] = []
        for day in sorted((path for path in stream.iterdir() if path.is_dir()), reverse=True):
            shards.extend(sorted(day.glob("*.parquet"), reverse=True))
            if len(shards) * FLUSH_EVERY >= limit * 2:
                break
        for path in shards:
            try:
                frame = pd.read_parquet(path)
            except Exception:
                logger.exception("failed to read dedup restore shard %s", path)
                continue
            if "ts_ms" not in frame.columns:
                continue
            ts = pd.to_numeric(frame["ts_ms"], errors="coerce")
            frame = frame.loc[ts <= through_ms]
            if frame.empty:
                continue
            frames.append(frame)
            rows += len(frame)
            if rows >= limit * 2:
                break
        if not frames:
            return pd.DataFrame()
        recent = pd.concat(frames, ignore_index=True)
        recent["ts_ms"] = pd.to_numeric(recent["ts_ms"], errors="raise").astype("int64")
        recent = recent.sort_values("ts_ms", kind="stable").tail(limit * 2)
        return recent.reset_index(drop=True)

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
            recent = self._recent_trade_frame(
                tick_root,
                self.exchange,
                symbol,
                through_ms=through_ms,
            )
            if not recent.empty:
                _recent_rows, recent_keys, recent_bodies, _duplicates = (
                    self._dedupe_replay_frame(
                        recent,
                        symbol=symbol,
                        conflict_root=tick_root,
                        exchange=self.exchange,
                    )
                )
                ordered_keys = sorted(
                    recent_keys,
                    key=lambda key: int(key.split(":", 1)[0]),
                )[-_SEEN_TRADE_IDS:]
                wanted_ids = {key.split(":", 1)[1] for key in ordered_keys}
                self.restored_trade_keys[symbol] = set(ordered_keys)
                self.restored_trade_bodies[symbol] = {
                    trade_id: body
                    for trade_id, body in recent_bodies.items()
                    if trade_id in wanted_ids
                }
                self.restored_last_trade_ts_ms[symbol] = int(recent["ts_ms"].max())
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
            no_id_count = sum(
                _trade_id(row) is None for row in frame.to_dict("records")
            )
            frame, keys, bodies, duplicate_count = self._dedupe_replay_frame(
                frame,
                symbol=symbol,
                conflict_root=tick_root,
                exchange=self.exchange,
            )
            self.restored_trade_metrics[symbol]["trades_dup_replay"] += duplicate_count
            self.restored_trade_metrics[symbol]["trades_no_id"] += no_id_count
            if duplicate_count:
                logger.info(
                    "forming recovery removed %d identical trade replay rows for %s",
                    duplicate_count,
                    symbol,
                )
            trades: list[Trade] = []
            for row in frame.to_dict("records"):
                side = str(row.get("side") or "").lower()
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
            merged_keys = sorted(
                self.restored_trade_keys[symbol] | keys,
                key=lambda key: int(key.split(":", 1)[0]),
            )[-_SEEN_TRADE_IDS:]
            self.restored_trade_keys[symbol] = set(merged_keys)
            wanted_ids = {key.split(":", 1)[1] for key in merged_keys}
            self.restored_trade_bodies[symbol].update(bodies)
            self.restored_trade_bodies[symbol] = {
                trade_id: body
                for trade_id, body in self.restored_trade_bodies[symbol].items()
                if trade_id in wanted_ids
            }
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
            symbol: deque(
                sorted(
                    self._seen_trade_ids[symbol],
                    key=lambda key: int(key.split(":", 1)[0]),
                )[-_SEEN_TRADE_IDS:]
            )
            for symbol in symbols
        }
        self._seen_trade_bodies: dict[str, dict[str, TradeBody]] = {
            symbol: dict(
                self.candle_sink.restored_trade_bodies.get(symbol, {})
                if self.candle_sink is not None
                else {}
            )
            for symbol in symbols
        }
        self._trade_metrics: dict[str, dict[str, int]] = {
            symbol: dict(
                self.candle_sink.restored_trade_metrics.get(symbol, _empty_trade_metrics())
                if self.candle_sink is not None
                else _empty_trade_metrics()
            )
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
        self._pending_trade_bodies: dict[str, dict[str, TradeBody]] = {
            symbol: {} for symbol in symbols
        }
        self._max_seen_trade_ts_ms: dict[str, int] = {}
        self._trade_arrival_seq = 0
        if self.candle_sink is not None:
            self._last_trade_ts_ms.update(self.candle_sink.restored_last_trade_ts_ms)

    def _ensure_trade_state(self, symbol: str) -> None:
        if not hasattr(self, "_seen_trade_bodies"):
            self._seen_trade_bodies = {}
        if not hasattr(self, "_pending_trade_bodies"):
            self._pending_trade_bodies = {}
        if not hasattr(self, "_trade_metrics"):
            self._trade_metrics = {}
        self._seen_trade_ids.setdefault(symbol, set())
        self._seen_trade_order.setdefault(symbol, deque())
        self._seen_trade_bodies.setdefault(symbol, {})
        self._pending_trade_ids.setdefault(symbol, set())
        self._pending_trade_bodies.setdefault(symbol, {})
        self._trade_metrics.setdefault(symbol, _empty_trade_metrics())

    def _remember_trade(self, symbol: str, key: str | None, row: dict[str, Any]) -> None:
        if key is None:
            return
        self._ensure_trade_state(symbol)
        seen = self._seen_trade_ids[symbol]
        order = self._seen_trade_order[symbol]
        if len(order) >= _SEEN_TRADE_IDS:
            evicted = order.popleft()
            seen.discard(evicted)
            self._seen_trade_bodies[symbol].pop(evicted.split(":", 1)[1], None)
        order.append(key)
        seen.add(key)
        trade_id = _trade_id(row)
        if trade_id is not None:
            self._seen_trade_bodies[symbol][trade_id] = _trade_body(row)

    def _trade_conflict(
        self,
        symbol: str,
        *,
        trade_id: str,
        previous: TradeBody,
        incoming: TradeBody,
        layer: str,
    ) -> None:
        self._trade_metrics[symbol]["trades_conflict"] += 1
        logger.error(
            "trade identity conflict: exchange=%s symbol=%s id=%s layer=%s "
            "previous=%s incoming=%s",
            getattr(self, "exchange_id", "unknown"),
            symbol,
            trade_id,
            layer,
            previous,
            incoming,
        )
        root = getattr(self, "root", None)
        if root is not None:
            _append_trade_conflict(
                Path(root),
                exchange=getattr(self, "exchange_id", "unknown"),
                symbol=symbol,
                trade_id=trade_id,
                previous=previous,
                incoming=incoming,
                layer=layer,
            )

    def trade_metrics_snapshot(self) -> dict[str, dict[str, int | float]]:
        return {
            canonical_symbol(symbol): {
                **counters,
                "seen_set_size": len(self._seen_trade_ids.get(symbol, ())),
                "reorder_bound_ms": _TRADE_REORDER_MS,
                "max_seen_event_ts_ms": self._max_seen_trade_ts_ms.get(symbol, 0),
                "watermark_event_ts_ms": max(
                    0, self._max_seen_trade_ts_ms.get(symbol, 0) - _TRADE_REORDER_MS
                ),
                "released_through_ts_ms": self._last_trade_ts_ms.get(symbol, 0),
                "pending_reorder": len(self._trade_reorder.get(symbol, ())),
            }
            for symbol, counters in self._trade_metrics.items()
        }

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
        self._ensure_trade_state(symbol)
        metrics = self._trade_metrics[symbol]
        metrics["trades_in"] += len(trades)
        candidates, malformed = _normalize_trade_batch(trades)
        late = 0
        duplicate = 0
        last_timestamp = self._last_trade_ts_ms.get(symbol)
        seen = self._seen_trade_ids[symbol]
        seen_bodies = self._seen_trade_bodies[symbol]
        pending_ids = self._pending_trade_ids[symbol]
        pending_bodies = self._pending_trade_bodies[symbol]
        pending = self._trade_reorder[symbol]
        for row, key in candidates:
            timestamp = int(row["ts_ms"])
            trade_id = _trade_id(row)
            if trade_id is None:
                metrics["trades_no_id"] += 1
            else:
                body = _trade_body(row)
                pending_body = pending_bodies.get(trade_id)
                if pending_body is not None:
                    if pending_body != body:
                        self._trade_conflict(
                            symbol,
                            trade_id=trade_id,
                            previous=pending_body,
                            incoming=body,
                            layer="live_pending",
                        )
                    else:
                        metrics["trades_dup_pending"] += 1
                    duplicate += 1
                    continue
                seen_body = seen_bodies.get(trade_id)
                if seen_body is not None and seen_body != body:
                    self._trade_conflict(
                        symbol,
                        trade_id=trade_id,
                        previous=seen_body,
                        incoming=body,
                        layer="live_seen",
                    )
                    duplicate += 1
                    continue
                if key in seen:
                    metrics["trades_dup_ws"] += 1
                    duplicate += 1
                    continue
            if last_timestamp is not None and timestamp < last_timestamp:
                late += 1
                metrics["trades_late_closed"] += 1
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
                assert trade_id is not None
                pending_ids.add(key)
                pending_bodies[trade_id] = _trade_body(row)
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
        pending_bodies = self._pending_trade_bodies[symbol]
        late = 0
        candle_rejected = 0
        last_timestamp = self._last_trade_ts_ms.get(symbol)
        while pending and (force or (through_ms is not None and pending[0][0] <= through_ms)):
            timestamp, _, row, key = heapq.heappop(pending)
            if key is not None:
                pending_ids.discard(key)
                trade_id = _trade_id(row)
                if trade_id is not None:
                    pending_bodies.pop(trade_id, None)
            if last_timestamp is not None and timestamp < last_timestamp:
                late += 1
                self._trade_metrics[symbol]["trades_late_closed"] += 1
                continue
            side = str(row.get("side") or "").lower()
            public_trade = PublicTrade(
                exchange=getattr(self, "exchange_id", buf.exchange),
                symbol=symbol,
                trade_id=_trade_id(row),
                timestamp=datetime.fromtimestamp(timestamp / 1000, tz=UTC),
                price=row["price"],
                amount=row["amount"],
                is_buyer_maker=(
                    False if side == "buy" else True if side == "sell" else None
                ),
            )
            public_trade.validate_clock(datetime.now(UTC))
            durable_row = public_trade.storage_row()
            # Preserve the venue integer exactly. A millisecond routed through
            # datetime.timestamp() can round 1001ms down to 1000ms, corrupting
            # the dedup body and making an exact reconnect look like a conflict.
            durable_row["ts_ms"] = timestamp
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
            latency = getattr(self, "recorder_latency", None)
            if latency is not None:
                latency.record(
                    "reorder_release_lag_ms",
                    max(0.0, float(int(datetime.now(UTC).timestamp() * 1000) - timestamp)),
                )
            self._remember_trade(symbol, key, durable_row)
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
            _write_trade_metrics(
                self.root,
                exchange=self.exchange_id,
                metrics=self.trade_metrics_snapshot(),
            )
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
                _write_trade_metrics(
                    self.root,
                    exchange=self.exchange_id,
                    metrics=self.trade_metrics_snapshot(),
                )
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
        self._seen_trade_ids: dict[str, set[str]] = {
            symbol: set(
                self.candle_sink.restored_trade_keys.get(symbol, set())
                if self.candle_sink is not None
                else ()
            )
            for symbol in self.symbols
        }
        self._seen_trade_order: dict[str, deque[str]] = {
            symbol: deque(
                sorted(
                    self._seen_trade_ids[symbol],
                    key=lambda key: int(key.split(":", 1)[0]),
                )[-_SEEN_TRADE_IDS:]
            )
            for symbol in self.symbols
        }
        self._seen_trade_bodies: dict[str, dict[str, TradeBody]] = {
            symbol: dict(
                self.candle_sink.restored_trade_bodies.get(symbol, {})
                if self.candle_sink is not None
                else {}
            )
            for symbol in self.symbols
        }
        self._trade_metrics: dict[str, dict[str, int]] = {
            symbol: dict(
                self.candle_sink.restored_trade_metrics.get(symbol, _empty_trade_metrics())
                if self.candle_sink is not None
                else _empty_trade_metrics()
            )
            for symbol in self.symbols
        }
        self._trade_bufs = {s: _Buffer(root, exchange_id, s, "trades") for s in self.symbols}
        self._book_bufs = {s: _Buffer(root, exchange_id, s, "book") for s in self.symbols}
        self._trade_reorder: dict[
            str, list[tuple[int, int, dict[str, Any]]]
        ] = {symbol: [] for symbol in self.symbols}
        self._max_seen_trade_ts_ms: dict[str, int] = {}
        self._last_trade_ts_ms: dict[str, int] = {}
        if self.candle_sink is not None:
            self._last_trade_ts_ms.update(self.candle_sink.restored_last_trade_ts_ms)
        self._trade_arrival_seq = 0
        channels = tuple(
            channel
            for channel, enabled in (
                ("ob_l1", not trades_only),
                ("trades", not books_only),
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

    @staticmethod
    def _safe_advance_boundary(now: datetime) -> datetime:
        """Candle clock bounded behind the trade reorder watermark."""
        return now - timedelta(milliseconds=_TRADE_REORDER_MS)

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
                    source=f"{self.exchange_id}:ob_l1",
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
        metrics = self._trade_metrics[sym]
        metrics["trades_in"] += 1
        raw_trade_id = trade.get("trade_id")
        trade_id = None if raw_trade_id in (None, "") else str(raw_trade_id)
        side = str(trade.get("side") or "").lower()
        try:
            timestamp_ms = int(trade["ts_ms"])
            price = Decimal(str(trade["price"]))
            amount = Decimal(str(trade["size"]))
            normalized: dict[str, Any] = {
                "ts_ms": timestamp_ms,
                "price": float(price),
                "amount": float(amount),
                "side": side,
                "trade_id": trade_id,
            }
            public_trade = PublicTrade(
                exchange=self.exchange_id,
                symbol=sym,
                trade_id=trade_id,
                timestamp=datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC),
                price=price,
                amount=amount,
                is_buyer_maker=(
                    False if side == "buy" else True if side == "sell" else None
                ),
            )
            public_trade.validate_clock(datetime.now(UTC))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            logger.warning("%s trade skipped: %s", sym, exc)
            return
        key = f"{timestamp_ms}:{trade_id}" if trade_id is not None else None
        if trade_id is None:
            metrics["trades_no_id"] += 1
        else:
            body = _trade_body(normalized)
            previous = self._seen_trade_bodies[sym].get(trade_id)
            if previous is not None and previous != body:
                metrics["trades_conflict"] += 1
                logger.error(
                    "Delta trade identity conflict: symbol=%s id=%s previous=%s incoming=%s",
                    sym,
                    trade_id,
                    previous,
                    body,
                )
                _append_trade_conflict(
                    self.root,
                    exchange=self.exchange_id,
                    symbol=sym,
                    trade_id=trade_id,
                    previous=previous,
                    incoming=body,
                    layer="delta_live",
                )
                return
            if key in self._seen_trade_ids[sym]:
                metrics["trades_dup_ws"] += 1
                return
        row = public_trade.storage_row()
        row["ts_ms"] = timestamp_ms
        if key is not None:
            assert trade_id is not None
            order = self._seen_trade_order[sym]
            seen = self._seen_trade_ids[sym]
            if len(order) >= _SEEN_TRADE_IDS:
                evicted = order.popleft()
                seen.discard(evicted)
                self._seen_trade_bodies[sym].pop(evicted.split(":", 1)[1], None)
            seen.add(key)
            order.append(key)
            self._seen_trade_bodies[sym][trade_id] = _trade_body(normalized)
        self._trade_arrival_seq += 1
        heapq.heappush(
            self._trade_reorder[sym],
            (timestamp_ms, self._trade_arrival_seq, row),
        )
        self._max_seen_trade_ts_ms[sym] = max(
            timestamp_ms,
            self._max_seen_trade_ts_ms.get(sym, timestamp_ms),
        )
        self._drain_delta_reorder(
            sym,
            through_ms=self._max_seen_trade_ts_ms[sym] - _TRADE_REORDER_MS,
        )

    def _drain_delta_reorder(
        self,
        sym: str,
        *,
        through_ms: int | None = None,
        force: bool = False,
    ) -> None:
        """Apply Delta prints in bounded event-time order before candle build."""
        pending = self._trade_reorder[sym]
        last_timestamp = self._last_trade_ts_ms.get(sym)
        buf = self._trade_bufs[sym]
        while pending and (force or (through_ms is not None and pending[0][0] <= through_ms)):
            timestamp_ms, _, row = heapq.heappop(pending)
            if last_timestamp is not None and timestamp_ms < last_timestamp:
                self._trade_metrics[sym]["trades_late_closed"] += 1
                continue
            self.recorder_latency.record(
                TRADE_INGEST_MS,
                max(0.0, float(self._epoch_ms() - timestamp_ms)),
            )
            # Raw trades are durable before a boundary print may publish the
            # previous candle, matching the generic recorder contract.
            buf.add(row)
            if self.candle_sink is not None and self.candle_sink.would_publish_on_trade(
                sym, timestamp_ms
            ):
                buf.flush((self._clock or time.monotonic)())
            if self.candle_sink is not None:
                try:
                    self.candle_sink.on_trade(
                        sym,
                        {
                            "timestamp": row["ts_ms"],
                            "price": row["price"],
                            "amount": row["amount"],
                            "side": row["side"],
                        },
                    )
                except ValueError as exc:
                    self._trade_metrics[sym]["trades_late_closed"] += 1
                    logger.warning(
                        "canonical Delta trade skipped: symbol=%s timestamp=%s reason=%s",
                        sym,
                        timestamp_ms,
                        exc,
                    )
                    last_timestamp = timestamp_ms
                    continue
            self.trade_count += 1
            last_timestamp = timestamp_ms
        if last_timestamp is not None:
            self._last_trade_ts_ms[sym] = last_timestamp

    def trade_metrics_snapshot(self) -> dict[str, dict[str, int | float]]:
        return {
            canonical_symbol(symbol): {
                **counters,
                "seen_set_size": len(self._seen_trade_ids.get(symbol, ())),
                "reorder_bound_ms": _TRADE_REORDER_MS,
                "max_seen_event_ts_ms": self._max_seen_trade_ts_ms.get(symbol, 0),
                "watermark_event_ts_ms": max(
                    0, self._max_seen_trade_ts_ms.get(symbol, 0) - _TRADE_REORDER_MS
                ),
                "released_through_ts_ms": self._last_trade_ts_ms.get(symbol, 0),
                "pending_reorder": len(self._trade_reorder.get(symbol, ())),
            }
            for symbol, counters in self._trade_metrics.items()
        }

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
                wall_now = datetime.now(UTC)
                # The candle clock must trail the exact same reorder watermark
                # as trade release.  Advancing at wall-clock ``:00`` closed the
                # minute before prints from its final 250 ms were eligible to
                # leave the heap, so otherwise-valid boundary trades were then
                # rejected as belonging to an already-closed candle.
                safe_boundary = self._safe_advance_boundary(wall_now)
                through_ms = int(safe_boundary.timestamp() * 1000)
                for symbol in self.symbols:
                    self._drain_delta_reorder(symbol, through_ms=through_ms)
                for buf in self._all_buffers():
                    if buf.should_flush(now):
                        buf.flush(now)
                if self.candle_sink is not None and self.candle_sink.would_publish_on_advance(
                    safe_boundary
                ):
                    for trade_buf in self._trade_bufs.values():
                        trade_buf.flush(now)
                if self.candle_sink is not None:
                    self.candle_sink.advance_time(safe_boundary)
                if now >= next_latency_snapshot:
                    self.recorder_latency_store.save_from(self.recorder_latency)
                    _write_trade_metrics(
                        self.root,
                        exchange=self.exchange_id,
                        metrics=self.trade_metrics_snapshot(),
                    )
                    next_latency_snapshot = now + 5.0
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            now = clock()
            for symbol in self.symbols:
                self._drain_delta_reorder(symbol, force=True)
            for buf in self._all_buffers():
                buf.flush(now)
            raise
        finally:
            try:
                self.recorder_latency_store.save_from(self.recorder_latency)
                _write_trade_metrics(
                    self.root,
                    exchange=self.exchange_id,
                    metrics=self.trade_metrics_snapshot(),
                )
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
