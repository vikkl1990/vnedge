"""Bootstrap canonical candles from durable trade-tick shards.

The live recorder owns forward collection. This utility is the deterministic
replay bridge used after a fresh deployment so Market Pulse can read recent
closed hours immediately. It never consumes exchange OHLCV and never invents
empty bars.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq

from vnedge.data.candles import Candle, CandleParquetStore, CandlePipeline
from vnedge.data.parquet_store import sanitize_symbol

_GAPFILL_NAME = re.compile(r"^\d+-gapfill-(\d+)-[0-9a-f]+\.parquet$")


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    symbols: int
    shards: int
    trades: int
    rejected: int
    candles: int


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _symbol_key(symbol: str) -> str:
    return symbol.split(":", 1)[0].replace("/", "")


def trade_shards(
    data_root: Path | str,
    source_exchange: str,
    symbol: str,
    *,
    days: int,
) -> tuple[Path, ...]:
    """Return shards from the newest N available UTC-day directories."""
    root = (
        Path(data_root)
        / "ticks"
        / f"exchange={source_exchange}"
        / f"symbol={sanitize_symbol(_symbol_key(symbol))}"
        / "stream=trades"
    )
    day_dirs = (
        sorted(
            (path for path in root.iterdir() if path.is_dir() and path.name.isdigit()),
            key=lambda path: path.name,
        )
        if root.is_dir()
        else []
    )
    selected = day_dirs[-max(1, days) :]
    return tuple(path for day in selected for path in sorted(day.glob("*.parquet")))


def _rows(path: Path) -> Iterable[tuple[int, object, object, str]]:
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(
        batch_size=100_000,
        columns=["ts_ms", "price", "amount", "side"],
    ):
        frame = batch.to_pandas().sort_values("ts_ms", kind="stable")
        for row in frame.itertuples(index=False):
            yield int(row.ts_ms), row.price, row.amount, str(row.side or "")


def _gapfill_interval(path: Path) -> tuple[int, int] | None:
    """Return the authoritative half-open interval encoded by a gapfill."""
    match = _GAPFILL_NAME.match(path.name)
    if match is None:
        return None
    start_ms = int(path.name.split("-", 1)[0])
    return start_ms, int(match.group(1))


def bootstrap_candles(
    data_root: Path | str,
    candle_root: Path | str,
    *,
    source_exchange: str,
    target_exchange: str,
    symbols: Iterable[str],
    days: int = 3,
    close_through: datetime | None = None,
) -> BootstrapReport:
    """Replay recent shards and atomically upsert closed canonical candles."""
    total_shards = total_trades = total_rejected = total_candles = 0
    symbol_count = 0
    store = CandleParquetStore(candle_root, exchange=target_exchange)
    boundary = close_through or datetime.now(UTC)

    for symbol in symbols:
        symbol_count += 1
        canonical = _symbol_key(symbol)
        captured: list[Candle] = []
        pipeline = CandlePipeline(canonical, subscribers=(captured.append,))
        shards = trade_shards(data_root, source_exchange, symbol, days=days)
        authoritative_intervals = tuple(
            interval for shard in shards if (interval := _gapfill_interval(shard)) is not None
        )
        total_shards += len(shards)
        last_timestamp: int | None = None
        for shard in shards:
            for ts_ms, price, amount, side in _rows(shard):
                if _gapfill_interval(shard) is None and any(
                    start_ms <= ts_ms < end_ms for start_ms, end_ms in authoritative_intervals
                ):
                    # The strict REST recovery is complete for this interval;
                    # ignore the websocket's partial overlap after a restart.
                    continue
                if last_timestamp is not None and ts_ms < last_timestamp:
                    total_rejected += 1
                    continue
                last_timestamp = ts_ms
                buyer_maker = (
                    False if side.lower() == "buy" else True if side.lower() == "sell" else None
                )
                try:
                    pipeline.on_trade(
                        datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                        Decimal(str(price)),
                        Decimal(str(amount)),
                        buyer_maker,
                    )
                    total_trades += 1
                except ValueError:
                    total_rejected += 1
        pipeline.advance_time(boundary)
        if captured:
            store.upsert(captured)
            total_candles += len(captured)

    return BootstrapReport(
        symbols=symbol_count,
        shards=total_shards,
        trades=total_trades,
        rejected=total_rejected,
        candles=total_candles,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="build canonical candles from recent durable trade shards"
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--candle-root", default="data/candles")
    parser.add_argument("--source-exchange", default="binanceusdm_hist")
    parser.add_argument("--target-exchange", default="binanceusdm")
    parser.add_argument(
        "--symbols",
        default="BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT",
    )
    parser.add_argument("--days", type=int, default=3)
    args = parser.parse_args(argv)
    symbols = _csv(args.symbols)
    if not symbols:
        parser.error("--symbols must name at least one symbol")
    report = bootstrap_candles(
        args.data_root,
        args.candle_root,
        source_exchange=args.source_exchange,
        target_exchange=args.target_exchange,
        symbols=symbols,
        days=args.days,
    )
    print(
        "canonical candle bootstrap: "
        f"{report.symbols} symbols, {report.shards} shards, "
        f"{report.trades} trades, {report.candles} candles, "
        f"{report.rejected} rejected"
    )
    return 1 if report.rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
