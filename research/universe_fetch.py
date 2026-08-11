"""Fetch a cross-sectional universe: daily candles + funding for 10 liquid perps.

The bot has only ever tested SINGLE-ASSET, DIRECTIONAL strategies. Cross-sectional
(relative-value, market-neutral) is the biggest untested category in crypto and
the one a fresh quant reaches for first — it removes both the direction-prediction
problem and the dominant BTC-beta noise. This pulls the raw material for it.
"""
import asyncio
import sys

import pandas as pd

sys.path.insert(0, "src")
from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.schemas import normalize_funding

OUT = "research/universe"
import os
os.makedirs(OUT, exist_ok=True)
SINCE = int(pd.Timestamp("2023-01-01", tz="UTC").timestamp() * 1000)
UNTIL = int(pd.Timestamp("2026-07-01", tz="UTC").timestamp() * 1000)
SYMS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC"]
COLS = ["timestamp", "open", "high", "low", "close", "volume"]


async def main() -> None:
    async with CcxtPublicClient("binanceusdm") as rest:
        for tag in SYMS:
            sym = f"{tag}/USDT:USDT"
            try:
                rows = await rest.fetch_candles(sym, "1d", SINCE, UNTIL)
                c = pd.DataFrame(rows, columns=COLS)
                c["timestamp"] = pd.to_datetime(c["timestamp"], unit="ms", utc=True)
                c = c.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
                c.to_parquet(f"{OUT}/{tag}_1d.parquet")
                f = normalize_funding(await rest.fetch_funding_history(sym, SINCE, UNTIL))
                f.to_parquet(f"{OUT}/{tag}_funding.parquet")
                print(f"  {tag}: {len(c)} daily bars ({c['timestamp'].iloc[0].date()}→{c['timestamp'].iloc[-1].date()}), {len(f)} funding")
            except Exception as e:
                print(f"  {tag}: SKIP ({e})")


asyncio.run(main())
