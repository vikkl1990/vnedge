from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from vnedge.data.candles import Candle, CandleParquetStore
from vnedge.data.canonical_repair import (
    CanonicalRepairConflictError,
    merge_staged_candles,
)


def candle(hour: int, close: str = "101") -> Candle:
    opened = datetime(2026, 8, 20, hour, tzinfo=UTC)
    return Candle(
        symbol="BTCUSDT",
        timeframe="1h",
        open_time=opened,
        close_time=opened + timedelta(hours=1),
        open=Decimal(100),
        high=Decimal(102),
        low=Decimal(99),
        close=Decimal(close),
        volume=Decimal(10),
        quote_volume=Decimal(1005),
        trade_count=10,
        taker_buy_volume=Decimal(6),
        vwap=Decimal("100.5"),
    )


def test_staged_repair_inserts_once_and_is_idempotent(tmp_path) -> None:
    stage_root = tmp_path / "stage"
    canonical_root = tmp_path / "canonical"
    CandleParquetStore(stage_root, exchange="binanceusdm").upsert((candle(1),))

    first = merge_staged_candles(
        staging_root=stage_root,
        canonical_root=canonical_root,
        authority_root=tmp_path,
        exchange="binanceusdm",
        symbols=["BTC/USDT:USDT"],
    )
    second = merge_staged_candles(
        staging_root=stage_root,
        canonical_root=canonical_root,
        authority_root=tmp_path,
        exchange="binanceusdm",
        symbols=["BTCUSDT"],
    )
    assert first.inserted_rows == 1
    assert second.inserted_rows == 0
    assert second.identical_rows == 1
    assert CandleParquetStore(canonical_root, exchange="binanceusdm").read(
        "BTCUSDT", "1h"
    ) == [candle(1)]


def test_staged_repair_conflict_aborts_before_any_insert(tmp_path) -> None:
    stage_root = tmp_path / "stage"
    canonical_root = tmp_path / "canonical"
    canonical = CandleParquetStore(canonical_root, exchange="binanceusdm")
    canonical.upsert((candle(1),))
    CandleParquetStore(stage_root, exchange="binanceusdm").upsert(
        (candle(1, close="100"), candle(2))
    )

    with pytest.raises(CanonicalRepairConflictError):
        merge_staged_candles(
            staging_root=stage_root,
            canonical_root=canonical_root,
            authority_root=tmp_path,
            exchange="binanceusdm",
            symbols=["BTCUSDT"],
        )
    assert canonical.read_at("BTCUSDT", "1h", candle(2).open_time) is None
