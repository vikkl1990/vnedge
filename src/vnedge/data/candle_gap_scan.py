"""Candle-sequence gap scanner — makes ``data_quality`` evidential.

The gap machinery in :mod:`vnedge.data.gaps` has no producer in the deployed
topology: ``GapAwareCandlePipeline`` is never constructed by the recorder, so
``GapParquetStore`` stays empty and every dashboard integrity claim reduces to
"ok by construction".  On 2026-08-19 that showed up live: BTC, ETH and SOL had
all silently lost the 17 Aug 15:00 hour while the cockpit reported ``ok``.

This module closes the loop from the other side.  Rather than changing live
ingestion -- ``GapAwareCandlePipeline.on_trade`` *withholds* trades whenever
the guard reports degraded, so a false positive would drop real tape -- it
reads the stored candles and records the holes it finds.  Read-only on the
candle lake, append-only on the gap store, safe to run repeatedly.

A hole is a missing bar: bar ``i`` is discontinuous when its ``open_time`` is
later than bar ``i-1``'s ``close_time``.  That test needs no external truth and
cannot be fooled by an empty gap store.

Usage:
    python -m vnedge.data.candle_gap_scan --exchange binanceusdm \
        --symbols BTCUSDT,ETHUSDT --timeframe 1h
"""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from vnedge.data.candles import Candle, CandleParquetStore
from vnedge.data.gaps import GapKind, GapParquetStore, GapRecord

DEFAULT_CANDLE_ROOT = Path("data/candles")
DEFAULT_GAP_ROOT = Path("data/gaps")


def find_candle_holes(
    candles: Sequence[Candle],
    *,
    exchange: str,
    symbol: str,
    detected_at: datetime,
) -> list[GapRecord]:
    """One STORAGE_HOLE record per discontinuity in the stored sequence."""
    records: list[GapRecord] = []
    for previous, current in pairwise(candles):
        if current.open_time <= previous.close_time:
            continue
        missing_minutes = (current.open_time - previous.close_time).total_seconds() / 60
        identity = (
            f"{exchange}|{symbol}|{GapKind.STORAGE_HOLE.value}|"
            f"{previous.close_time.isoformat()}|{current.open_time.isoformat()}"
        )
        records.append(
            GapRecord(
                symbol=symbol,
                exchange=exchange,
                kind=GapKind.STORAGE_HOLE,
                start=previous.close_time,
                end=current.open_time,
                detected_at=detected_at,
                detail=(
                    f"candle sequence hole: {missing_minutes:.0f} min missing between "
                    f"{previous.close_time.isoformat()} and {current.open_time.isoformat()}"
                ),
                # A periodic scan observes the same immutable hole many times.
                # Its identity is the interval, not the observation timestamp.
                gap_id=hashlib.sha256(identity.encode()).hexdigest()[:24],
            )
        )
    return records


def scan(
    *,
    exchange: str,
    symbols: Sequence[str],
    timeframe: str = "1h",
    candle_root: Path = DEFAULT_CANDLE_ROOT,
    gap_root: Path = DEFAULT_GAP_ROOT,
    write: bool = True,
    now: datetime | None = None,
) -> dict[str, list[GapRecord]]:
    detected_at = now or datetime.now(UTC)
    store = CandleParquetStore(candle_root, exchange=exchange)
    gap_store = GapParquetStore(gap_root)
    found: dict[str, list[GapRecord]] = {}
    for symbol in symbols:
        candles = store.read(symbol, timeframe)
        holes = find_candle_holes(
            candles, exchange=exchange, symbol=symbol, detected_at=detected_at
        )
        found[symbol] = holes
        if holes and write:
            gap_store.upsert(holes)
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exchange", default="binanceusdm")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--candle-root", type=Path, default=DEFAULT_CANDLE_ROOT)
    parser.add_argument("--gap-root", type=Path, default=DEFAULT_GAP_ROOT)
    parser.add_argument("--dry-run", action="store_true",
                        help="report holes without writing gap records")
    args = parser.parse_args()

    found = scan(
        exchange=args.exchange,
        symbols=[s.strip() for s in args.symbols.split(",") if s.strip()],
        timeframe=args.timeframe,
        candle_root=args.candle_root,
        gap_root=args.gap_root,
        write=not args.dry_run,
    )
    total = sum(len(v) for v in found.values())
    mode = "would record" if args.dry_run else "recorded"
    print(f"candle gap scan [{args.exchange} {args.timeframe}] -- {mode} {total} hole(s)")
    for symbol, holes in found.items():
        if not holes:
            print(f"  {symbol}: contiguous")
            continue
        for hole in holes:
            minutes = (hole.end - hole.start).total_seconds() / 60
            print(f"  {symbol}: {hole.start:%Y-%m-%d %H:%M} -> {hole.end:%H:%M} "
                  f"({minutes:.0f} min)")


if __name__ == "__main__":
    main()
