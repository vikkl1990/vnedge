from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from vnedge.data.candle_bootstrap import bootstrap_candles, trade_shards
from vnedge.data.candles import CandleParquetStore

START = datetime(2026, 8, 15, tzinfo=UTC)


def _write_shard(root, *, day: str, offset: int, minutes: range) -> None:
    directory = root / "ticks/exchange=binanceusdm_hist/symbol=BTCUSDT/stream=trades" / day
    directory.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "ts_ms": int((START + timedelta(minutes=minute)).timestamp() * 1000),
            "price": 60_000.0 + minute,
            "amount": 1.0,
            "side": "buy" if minute % 2 else "sell",
        }
        for minute in minutes
    ]
    pd.DataFrame(rows).to_parquet(directory / f"{rows[0]['ts_ms']}-{offset:06d}.parquet")


def test_bootstrap_replays_shards_into_closed_hour(tmp_path) -> None:
    _write_shard(tmp_path, day="20260815", offset=0, minutes=range(30))
    _write_shard(tmp_path, day="20260815", offset=1, minutes=range(30, 60))

    report = bootstrap_candles(
        tmp_path,
        tmp_path / "candles",
        source_exchange="binanceusdm_hist",
        target_exchange="binanceusdm",
        symbols=["BTC/USDT:USDT"],
        close_through=START + timedelta(hours=1),
    )

    assert report.shards == 2
    assert report.trades == 60
    assert report.rejected == 0
    hours = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm").read("BTCUSDT", "1h")
    assert len(hours) == 1
    assert hours[0].open == 60_000
    assert hours[0].close == 60_059
    assert hours[0].trade_count == 60
    assert hours[0].vwap is not None


def test_trade_shards_uses_newest_available_days(tmp_path) -> None:
    for index, day in enumerate(("20260813", "20260814", "20260815")):
        _write_shard(tmp_path, day=day, offset=index, minutes=range(index, index + 1))
    selected = trade_shards(
        tmp_path,
        "binanceusdm_hist",
        "BTC/USDT:USDT",
        days=2,
    )
    assert {path.parent.name for path in selected} == {"20260814", "20260815"}


def test_gapfill_tape_takes_precedence_over_partial_live_overlap(tmp_path) -> None:
    directory = tmp_path / "ticks/exchange=binanceusdm_hist/symbol=BTCUSDT/stream=trades/20260815"
    directory.mkdir(parents=True)
    start_ms = int(START.timestamp() * 1_000)
    end_ms = int((START + timedelta(hours=1)).timestamp() * 1_000)
    authoritative = pd.DataFrame(
        [
            {
                "ts_ms": start_ms + minute * 60_000,
                "price": 60_000.0 + minute,
                "amount": 1.0,
                "side": "buy",
            }
            for minute in range(60)
        ]
    )
    authoritative.to_parquet(
        directory / f"{start_ms}-gapfill-{end_ms}-abcdef.parquet",
        index=False,
    )
    # Simulate a recorder that restarted halfway through the same hour.
    authoritative.iloc[30:].to_parquet(
        directory / f"{start_ms + 30 * 60_000}-000001.parquet",
        index=False,
    )

    report = bootstrap_candles(
        tmp_path,
        tmp_path / "candles",
        source_exchange="binanceusdm_hist",
        target_exchange="binanceusdm",
        symbols=["BTC/USDT:USDT"],
        close_through=START + timedelta(hours=1),
    )

    assert report.trades == 60
    assert report.rejected == 0
    hour = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm").read("BTCUSDT", "1h")[0]
    assert hour.trade_count == 60
    assert hour.volume == 60
