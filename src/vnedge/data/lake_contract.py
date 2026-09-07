"""Audit and migrate the immutable candle-lake identity contract.

This command never downloads, repairs, or invents market data.  ``stamp``
only adds deterministic provenance metadata to existing canonical partitions;
official Delta backfill must be written separately with its explicit source.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from vnedge.data.candles import CandleParquetStore
from vnedge.data.parquet_store import sanitize_symbol
from vnedge.data.symbols import canonical_symbol


def audit_lake(
    root: Path,
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
) -> dict[str, object]:
    store = CandleParquetStore(root, exchange=exchange)
    records = store.read_records(symbol, timeframe)
    candles = [record.candle for record in records]
    decision_eligible = [
        record
        for record in records
        if record.identity_persisted
        and record.source == "canonical_tick_lake"
        and record.data_quality == "ok"
        and record.coverage_ok
    ]
    context_eligible = [
        record
        for record in records
        if record.identity_persisted
        and record.source in {"canonical_tick_lake", "official_delta_ohlc", "repaired"}
        and record.data_quality == "ok"
        and record.coverage_ok
    ]
    directory = (
        root
        / f"exchange={exchange.strip().lower()}"
        / sanitize_symbol(canonical_symbol(symbol))
        / timeframe
    )
    rows = 0
    stamped = 0
    sources: dict[str, int] = {}
    for path in sorted(directory.glob("*.parquet")):
        frame = pd.read_parquet(path)
        rows += len(frame)
        if "content_sha256" in frame:
            stamped += int(frame["content_sha256"].fillna("").astype(str).str.len().eq(64).sum())
        if "source" in frame:
            for name, count in frame["source"].fillna("unreported").value_counts().items():
                sources[str(name)] = sources.get(str(name), 0) + int(count)
    return {
        "exchange": exchange.strip().lower(),
        "symbol": canonical_symbol(symbol),
        "timeframe": timeframe,
        "bars": len(candles),
        "physical_rows": rows,
        "identity_stamped_rows": stamped,
        "identity_complete": bool(rows and stamped == rows),
        "decision_eligible_rows": len(decision_eligible),
        "context_eligible_rows": len(context_eligible),
        "first_open": candles[0].open_time.isoformat() if candles else None,
        "last_close": candles[-1].close_time.isoformat() if candles else None,
        "ema200_ready": timeframe == "1d" and len(context_eligible) >= 200,
        "sources": sources,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("audit", "stamp"))
    parser.add_argument("--root", type=Path, default=Path("data/candles"))
    parser.add_argument("--exchange", default="delta_india")
    parser.add_argument("--symbols", default="BTCUSD,ETHUSD")
    parser.add_argument("--timeframes", default="15m,4h,1d")
    parser.add_argument(
        "--source",
        default="canonical_tick_lake",
        choices=("canonical_tick_lake", "official_delta_ohlc", "repaired"),
    )
    args = parser.parse_args()
    rows: list[dict[str, object]] = []
    for symbol in (value.strip() for value in args.symbols.split(",") if value.strip()):
        for timeframe in (
            value.strip() for value in args.timeframes.split(",") if value.strip()
        ):
            store = CandleParquetStore(args.root, exchange=args.exchange)
            migrated = None
            if args.action == "stamp":
                migrated = store.stamp_legacy_partitions(
                    symbol,
                    timeframe,
                    source=args.source,
                )
            result = audit_lake(
                args.root,
                exchange=args.exchange,
                symbol=symbol,
                timeframe=timeframe,
            )
            if migrated is not None:
                result["rows_migrated"] = migrated
            rows.append(result)
    print(json.dumps({"schema_version": 2, "rows": rows}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = ["audit_lake"]
