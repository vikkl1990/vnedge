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
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

FLUSH_EVERY = 500       # records
FLUSH_SECONDS = 30.0
_BACKOFF = 2.0


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
            self._rows and now - self._last_flush >= FLUSH_SECONDS
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
                 *, levels: int = 10) -> None:
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
        self.trade_count = 0
        self.book_count = 0

    async def _watch_trades(self, symbol: str, clock) -> None:
        buf = _Buffer(self.root, self.exchange_id, symbol, "trades")
        while True:
            try:
                trades = await self._ex.watch_trades(symbol)
                for t in trades:
                    buf.add({
                        "ts_ms": int(t["timestamp"]),
                        "price": float(t["price"]),
                        "amount": float(t["amount"]),
                        "side": t.get("side", ""),
                    })
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
            tasks.append(asyncio.create_task(self._watch_book(symbol, clock)))
        logger.info("tick recorder: %s %s -> %s", self.exchange_id, self.symbols, self.root)
        try:
            await asyncio.gather(*tasks)
        finally:
            await self._ex.close()


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="zero-risk tick/book recorder")
    p.add_argument("--exchange", default="binanceusdm")
    p.add_argument("--symbols", default="BTC/USDT:USDT")
    p.add_argument("--data-root", default="data")
    p.add_argument("--levels", type=int, default=10, help="L2 depth levels per side")
    args = p.parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    recorder = TickRecorder(args.exchange, symbols, Path(args.data_root),
                            levels=args.levels)
    asyncio.run(recorder.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
