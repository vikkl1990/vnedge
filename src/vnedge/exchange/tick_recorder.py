"""Tick + top-of-book recorder — zero-risk data collection.

    python -m vnedge.exchange.tick_recorder --symbols BTC/USDT:USDT

Streams live trades and best bid/ask via CCXT Pro websockets and appends
them to daily Parquet files. NO execution, NO credentials, NO order code —
it only reads public streams and writes files. This is the data source the
true microstructure scalper backtest needs (candles can't approximate real
order flow); collect for a couple of weeks, then replay.

Batched writes (flush every N records or T seconds) keep memory bounded and
disk churn low. One daily file per stream per symbol; a crash loses at most
the un-flushed batch. Bounded-backoff reconnection.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

FLUSH_EVERY = 500       # records
FLUSH_SECONDS = 30.0
_BACKOFF = 2.0


class _Buffer:
    """Accumulates rows and appends to a daily Parquet file for one stream."""

    def __init__(self, root: Path, exchange: str, symbol: str, stream: str) -> None:
        self.root = root
        self.exchange = exchange
        self.symbol = symbol
        self.stream = stream
        self._rows: list[dict] = []
        self._last_flush = 0.0

    def _path(self, day: str) -> Path:
        safe = self.symbol.split(":")[0].replace("/", "")
        d = self.root / "ticks" / f"exchange={self.exchange}" / f"symbol={safe}" / f"stream={self.stream}"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{day}.parquet"

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
            path = self._path(day)
            chunk = chunk.drop(columns="_day")
            if path.exists():
                chunk = pd.concat([pd.read_parquet(path), chunk], ignore_index=True)
            chunk.to_parquet(path, index=False)
        self._rows.clear()
        self._last_flush = now
        return n


class TickRecorder:
    def __init__(self, exchange_id: str, symbols: list[str], root: Path) -> None:
        import ccxt.pro as ccxtpro

        if not hasattr(ccxtpro, exchange_id):
            raise ValueError(f"unknown CCXT Pro exchange id: {exchange_id}")
        self._ex = getattr(ccxtpro, exchange_id)({"enableRateLimit": True})
        self.exchange_id = exchange_id
        self.symbols = symbols
        self.root = root
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
                ob = await self._ex.watch_order_book(symbol, limit=5)
                if ob["bids"] and ob["asks"]:
                    buf.add({
                        "ts_ms": int(ob.get("timestamp") or clock() * 1000),
                        "bid": float(ob["bids"][0][0]),
                        "bid_qty": float(ob["bids"][0][1]),
                        "ask": float(ob["asks"][0][0]),
                        "ask_qty": float(ob["asks"][0][1]),
                    })
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
    args = p.parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    recorder = TickRecorder(args.exchange, symbols, Path(args.data_root))
    asyncio.run(recorder.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
