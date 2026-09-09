"""Read-only Arena adapter for the recorder's canonical Parquet partitions.

Unlike legacy candle readers this boundary never upgrades provenance, dedupes,
repairs gaps, or falls back to exchange OHLC. Missing proof stays missing.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from vnedge.data.parquet_store import sanitize_symbol
from vnedge.data.symbols import canonical_symbol


class CanonicalResearchStore:
    def __init__(self, root: Path | str, *, max_bars: int = 20_000) -> None:
        if not 1 <= max_bars <= 20_000:
            raise ValueError("canonical_research_bar_budget_invalid")
        self.root = Path(root)
        self.max_bars = max_bars

    def read_candles(self, exchange: str, symbol: str, timeframe: str) -> pd.DataFrame:
        if exchange != "delta_india" or symbol not in {"BTC/USD:USD", "ETH/USD:USD"}:
            raise ValueError("canonical_research_product_unsupported")
        if timeframe not in {"5m", "15m", "1h"}:
            raise ValueError("canonical_research_timeframe_unsupported")
        directory = self.root / f"exchange={exchange}" / sanitize_symbol(canonical_symbol(symbol)) / timeframe
        frames: list[pd.DataFrame] = []
        count = 0
        for path in reversed(sorted(directory.glob("*.parquet"))):
            frame = pd.read_parquet(path)
            frames.append(frame)
            count += len(frame)
            if count >= self.max_bars:
                break
        if not frames:
            raise ValueError("canonical_history_missing")
        frame = pd.concat(list(reversed(frames)), ignore_index=True).tail(self.max_bars).copy()
        required = {"open_time", "close_time", "source", "content_sha256",
                    "is_closed", "data_quality", "coverage_ok"}
        missing = sorted(required - set(frame.columns))
        if missing:
            raise ValueError("canonical_persisted_proof_missing:" + ",".join(missing))
        # Identity comes from the explicit venue/product/TF partition, not from
        # an arbitrary OHLC file. Reject contradictory row identity if present.
        for name, value in (("exchange", exchange), ("symbol", canonical_symbol(symbol)),
                            ("timeframe", timeframe)):
            if name in frame and not frame[name].eq(value).all():
                raise ValueError(f"canonical_partition_{name}_mismatch")
        frame["timestamp"] = frame["open_time"]
        frame["candle_source"] = frame["source"]
        frame["exchange"], frame["symbol"], frame["timeframe"] = exchange, symbol, timeframe
        # Recorder hashes the same float-normalized feature representation.
        # Preserve the persisted hash; preflight recomputes and compares it.
        for name in ("open", "high", "low", "close", "volume", "quote_volume",
                     "trade_count", "taker_buy_volume", "vwap"):
            if name in frame:
                frame[name] = frame[name].astype(float)
        frame["decision_transport"] = "parquet"
        return frame.reset_index(drop=True)
