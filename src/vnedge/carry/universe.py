"""Cross-sectional universe: deterministic membership + aligned data loading."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

# Static, documented membership (Phase 1.1). Liquid binanceusdm perps with full
# 2023+ history. Changing this set is a reviewed change, not a runtime toggle.
CARRY_UNIVERSE: tuple[str, ...] = (
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC",
)


def load_universe(
    data_dir: str | Path,
    symbols: tuple[str, ...] = CARRY_UNIVERSE,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load aligned daily close + daily funding wide frames.

    Returns (close, funding): DataFrames indexed by UTC day, columns = symbols.
    Funding is the daily SUM of settled prints (three 8h settlements/day),
    reindexed onto the close calendar. Missing funding → 0 (neutral).
    """
    data_dir = Path(data_dir)
    close: dict[str, pd.Series] = {}
    fund: dict[str, pd.Series] = {}
    for s in symbols:
        c = pd.read_parquet(data_dir / f"{s}_1d.parquet")
        c["timestamp"] = pd.to_datetime(c["timestamp"], utc=True)
        close[s] = c.set_index("timestamp")["close"]
        f = pd.read_parquet(data_dir / f"{s}_funding.parquet")
        f["timestamp"] = pd.to_datetime(f["timestamp"], utc=True)
        fund[s] = f.set_index("timestamp")["funding_rate"].resample("1D").sum()
    close_df = pd.DataFrame(close).sort_index()
    fund_df = pd.DataFrame(fund).reindex(close_df.index).fillna(0.0)
    return close_df, fund_df
