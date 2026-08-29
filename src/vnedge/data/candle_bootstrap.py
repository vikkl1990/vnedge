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

from vnedge.data.candles import Candle, CandleParquetStore, CandlePipeline, floor_time
from vnedge.data.parquet_store import sanitize_symbol
from vnedge.data.symbols import canonical_symbol

_GAPFILL_NAME = re.compile(r"^\d+-gapfill-(\d+)-[0-9a-f]+\.parquet$")


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    symbols: int
    shards: int
    trades: int
    rejected: int
    candles: int
    skipped_existing_minutes: int = 0


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _symbol_key(symbol: str) -> str:
    return canonical_symbol(symbol)


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


def _shard_interval(path: Path) -> tuple[int, int] | None:
    """Return a shard's known half-open coverage interval when available."""
    gapfill = _gapfill_interval(path)
    if gapfill is not None:
        return gapfill
    day = path.parent.name
    if len(day) != 8 or not day.isdigit():
        return None
    start = datetime.strptime(day, "%Y%m%d").replace(tzinfo=UTC)
    start_ms = int(start.timestamp() * 1_000)
    return start_ms, start_ms + 86_400_000


def _interval_minutes(start_ms: int, end_ms: int) -> set[int]:
    first = int(
        floor_time(datetime.fromtimestamp(start_ms / 1_000, tz=UTC), "1m").timestamp()
        * 1_000
    )
    return set(range(first, end_ms, 60_000))


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
    total_skipped_existing_minutes = 0
    symbol_count = 0
    store = CandleParquetStore(candle_root, exchange=target_exchange)
    boundary = close_through or datetime.now(UTC)

    for symbol in symbols:
        symbol_count += 1
        canonical = _symbol_key(symbol)
        existing_minute_ms = {
            int(candle.open_time.timestamp() * 1_000)
            for candle in store.read(canonical, "1m")
        }
        skipped_minutes: set[int] = set()
        captured: list[Candle] = []
        pipeline = CandlePipeline(canonical, subscribers=(captured.append,))
        shards = trade_shards(data_root, source_exchange, symbol, days=days)
        authoritative_intervals = tuple(
            interval for shard in shards if (interval := _gapfill_interval(shard)) is not None
        )
        total_shards += len(shards)
        last_timestamp: int | None = None
        for shard in shards:
            interval = _shard_interval(shard)
            if interval is not None:
                shard_minutes = _interval_minutes(*interval)
                if shard_minutes.issubset(existing_minute_ms):
                    # The whole authoritative shard is already canonical. Do
                    # not scan millions of trades merely to skip each row.
                    skipped_minutes.update(shard_minutes)
                    continue
            for ts_ms, price, amount, side in _rows(shard):
                if _gapfill_interval(shard) is None and any(
                    start_ms <= ts_ms < end_ms for start_ms, end_ms in authoritative_intervals
                ):
                    # The strict REST recovery is complete for this interval;
                    # ignore the websocket's partial overlap after a restart.
                    continue
                minute_ms = int(
                    floor_time(
                        datetime.fromtimestamp(ts_ms / 1_000, tz=UTC), "1m"
                    ).timestamp()
                    * 1_000
                )
                if minute_ms in existing_minute_ms:
                    skipped_minutes.add(minute_ms)
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
        # Re-run the store-backed parent repair even when no trades were
        # replayed.  This fills only missing complete 5m/15m/1h/4h parents
        # from the now-authoritative 1m base and never rebuilds existing rows.
        CandlePipeline(canonical, store=store)
        total_skipped_existing_minutes += len(skipped_minutes)

    return BootstrapReport(
        symbols=symbol_count,
        shards=total_shards,
        trades=total_trades,
        rejected=total_rejected,
        candles=total_candles,
        skipped_existing_minutes=total_skipped_existing_minutes,
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
        f"{report.rejected} rejected, "
        f"{report.skipped_existing_minutes} existing minutes skipped"
    )
    return 1 if report.rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
