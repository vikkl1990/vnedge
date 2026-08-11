"""Fetch BTC/ETH binance-perp funding history for the funding_squeeze windows."""
import asyncio
import sys

import pandas as pd

sys.path.insert(0, "src")
from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.schemas import normalize_funding

OUT = "research/htf_data"
SINCE = int(pd.Timestamp("2023-01-01", tz="UTC").timestamp() * 1000)
UNTIL = int(pd.Timestamp("2026-07-01", tz="UTC").timestamp() * 1000)


async def main() -> None:
    async with CcxtPublicClient("binanceusdm") as rest:
        for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT"]:
            raw = await rest.fetch_funding_history(sym, SINCE, UNTIL)
            df = normalize_funding(raw)
            tag = sym.split("/")[0]
            df.to_parquet(f"{OUT}/{tag}_funding.parquet")
            print(f"  {tag} funding: {len(df):>6} prints  {df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]}")


asyncio.run(main())
