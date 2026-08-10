"""Fetch BTC/ETH 1h+4h binance-perp candles for the htf_structure_break windows.

Public fetch_ohlcv only (no keys). Covers the frozen selection window
(2023-01 → 2025-06) AND the sealed tail (2025-07 → 2026-06). Having the tail on
disk is not 'peeking' — the discipline is that the backtest does not RUN on it
until §5 passes.
"""
import asyncio
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, "src")
from vnedge.data.ccxt_client import CcxtPublicClient

OUT = Path("research/htf_data")
OUT.mkdir(parents=True, exist_ok=True)
SINCE = int(pd.Timestamp("2023-01-01", tz="UTC").timestamp() * 1000)
UNTIL = int(pd.Timestamp("2026-07-01", tz="UTC").timestamp() * 1000)
COLS = ["timestamp", "open", "high", "low", "close", "volume"]


async def main() -> None:
    async with CcxtPublicClient("binanceusdm") as rest:
        for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT"]:
            for tf in ["1h", "4h"]:
                rows = await rest.fetch_candles(sym, tf, SINCE, UNTIL)
                df = pd.DataFrame(rows, columns=COLS)
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
                tag = sym.split("/")[0]
                df.to_parquet(OUT / f"{tag}_{tf}.parquet")
                print(f"  {tag} {tf}: {len(df):>6} bars  {df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]}")


asyncio.run(main())
