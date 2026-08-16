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
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from vnedge.data.candles import CandleParquetStore, CandlePipeline

logger = logging.getLogger(__name__)

FLUSH_EVERY = 500       # records
FLUSH_SECONDS = 30.0
_BACKOFF = 2.0
_DELTA_NATIVE_IDS = {"delta_india", "delta", "deltaindia"}


def _canonical_symbol(symbol: str) -> str:
    """Match the tick-lake/candle-store symbol key used by the dashboard."""
    return symbol.split(":", 1)[0].replace("/", "")


class CanonicalCandleSink:
    """Feed public trades into per-symbol canonical candle pipelines.

    This is measurement-only plumbing. It consumes the same public trades that
    are durably written to the tick lake and persists only closed candles.
    """

    def __init__(self, exchange: str, symbols: list[str], root: Path | str) -> None:
        store = CandleParquetStore(root, exchange=exchange)
        self.pipelines = {
            symbol: CandlePipeline(_canonical_symbol(symbol), store=store)
            for symbol in symbols
        }

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


def _book_row(ob: dict, levels: int, ts_ms: int) -> dict:
    """Flatten a CCXT order book into one L2 row: level-0 L1 aliases
    (bid/bid_qty/ask/ask_qty, kept for the top-of-book replay engine) plus the
    bid_px_i/bid_qty_i/ask_px_i/ask_qty_i ladder for i in [0, levels). Missing
    levels are padded with NaN price / 0.0 qty so the schema is fixed-width."""
    bids, asks = ob["bids"], ob["asks"]
    row = {
        "ts_ms": ts_ms,
        "bid": float(bids[0][0]), "bid_qty": float(bids[0][1]),
        "ask": float(asks[0][0]), "ask_qty": float(asks[0][1]),
    }
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
                 trades_only: bool = False) -> None:
        import ccxt.pro as ccxtpro

        if not hasattr(ccxtpro, exchange_id):
            raise ValueError(f"unknown CCXT Pro exchange id: {exchange_id}")
        if levels < 1:
            raise ValueError("levels must be >= 1")
        self._ex = getattr(ccxtpro, exchange_id)({"enableRateLimit": True})
        self.exchange_id = exchange_id
        self.symbols = symbols
        self.root = root
        self.levels = levels
        # depth-stream limit BOTH Binance USDT-M and Bybit swaps accept (Bybit
        # rejects 5/10/20 — only {1,50,200,1000}); we slice to `levels` on write.
        self._book_limit = 50 if levels <= 50 else 200
        self.trades_only = trades_only
        self.candle_sink = (
            CanonicalCandleSink(exchange_id, symbols, candle_root)
            if candle_root is not None
            else None
        )
        self.trade_count = 0
        self.book_count = 0

    async def _watch_trades(self, symbol: str, clock) -> None:
        buf = _Buffer(self.root, self.exchange_id, symbol, "trades")
        while True:
            try:
                trades = await self._ex.watch_trades(symbol)
                for t in trades:
                    row = {
                        "ts_ms": int(t["timestamp"]),
                        "price": float(t["price"]),
                        "amount": float(t["amount"]),
                        "side": t.get("side", ""),
                    }
                    buf.add(row)
                    if self.candle_sink is not None:
                        self.candle_sink.on_trade(symbol, t)
                    self.trade_count += 1
                now = clock()
                if buf.should_flush(now):
                    buf.flush(now)
            except asyncio.CancelledError:
                buf.flush(clock())
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s trades error: %s", symbol, exc)
                await asyncio.sleep(_BACKOFF)

    async def _advance_candles(self) -> None:
        assert self.candle_sink is not None
        while True:
            self.candle_sink.advance_time(datetime.now(UTC))
            await asyncio.sleep(1.0)

    async def _watch_book(self, symbol: str, clock) -> None:
        buf = _Buffer(self.root, self.exchange_id, symbol, "book")
        while True:
            try:
                ob = await self._ex.watch_order_book(symbol, limit=self._book_limit)
                if ob["bids"] and ob["asks"]:
                    ts_ms = int(ob.get("timestamp") or clock() * 1000)
                    buf.add(_book_row(ob, self.levels, ts_ms))
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

    async def run(self, clock=None) -> None:
        import time as _t

        clock = clock or _t.monotonic
        tasks = []
        for symbol in self.symbols:
            tasks.append(asyncio.create_task(self._watch_trades(symbol, clock)))
            if not self.trades_only:
                tasks.append(asyncio.create_task(self._watch_book(symbol, clock)))
        if self.candle_sink is not None:
            tasks.append(asyncio.create_task(self._advance_candles()))
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
        root = Path(root)
        self.exchange_id = exchange_id
        self.symbols = [delta_native_symbol(s) for s in symbols]
        self.root = root
        self.levels = levels
        self._clock = clock
        self.trade_count = 0
        self.book_count = 0
        self._trade_bufs = {
            s: _Buffer(root, exchange_id, s, "trades") for s in self.symbols
        }
        self._book_bufs = {
            s: _Buffer(root, exchange_id, s, "book") for s in self.symbols
        }
        self._client = DeltaPublicWsClient(
            self.symbols,
            channels=("l2_orderbook", "all_trades"),
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
            buf.add(_book_row(_delta_ob(buy, sell), self.levels, ts_ms))
        except (KeyError, TypeError, ValueError):
            return
        self.book_count += 1

    def _on_trade(self, sym: str, trade: dict) -> None:
        buf = self._trade_bufs.get(sym)
        if buf is None:
            return
        buf.add(
            {
                "ts_ms": int(trade["ts_ms"]),
                "price": float(trade["price"]),
                "amount": float(trade["size"]),
                "side": trade.get("side", ""),
            }
        )
        self.trade_count += 1

    def _all_buffers(self):
        return (*self._trade_bufs.values(), *self._book_bufs.values())

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
    p.add_argument("--levels", type=int, default=10, help="L2 depth levels per side")
    args = p.parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.exchange in _DELTA_NATIVE_IDS:
        recorder: DeltaTickRecorder | TickRecorder = DeltaTickRecorder(
            symbols, Path(args.data_root), levels=args.levels, exchange_id=args.exchange
        )
    else:
        recorder = TickRecorder(
            args.exchange,
            symbols,
            Path(args.data_root),
            levels=args.levels,
            candle_root=Path(args.candle_root) if args.candle_root else None,
            trades_only=args.trades_only,
        )
    asyncio.run(recorder.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
