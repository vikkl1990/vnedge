from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from vnedge.data.candles import (
    Candle,
    CandleAggregator,
    CandleBuilder,
    CandleParquetStore,
    CandlePipeline,
    JsonlTradeQuarantine,
    Trade,
    aggregate_candle_series,
    build_candles_from_trades,
    floor_time,
    merge_candles,
    trades_from_tick_frame,
)

D = Decimal
START = datetime(2026, 8, 16, tzinfo=UTC)


def candle_at(hour: int, *, close: str = "101") -> Candle:
    opened = START + timedelta(hours=hour)
    return Candle(
        symbol="BTC/USDT:USDT",
        timeframe="1h",
        open_time=opened,
        close_time=opened + timedelta(hours=1),
        open=D("100"),
        high=max(D("102"), D(close)),
        low=D("99"),
        close=D(close),
        volume=D("2"),
        quote_volume=D("201"),
        trade_count=2,
        taker_buy_volume=D("0.5"),
        vwap=D("100.5"),
    )


def test_floor_time_is_utc_epoch_aligned() -> None:
    local = datetime(2026, 8, 16, 14, 43, 21, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert floor_time(local, "1m") == datetime(2026, 8, 16, 9, 13, tzinfo=UTC)
    assert floor_time(local, "4h") == datetime(2026, 8, 16, 8, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        floor_time(datetime(2026, 8, 16), "1m")  # noqa: DTZ001 - intentional naive input
    with pytest.raises(ValueError, match="unsupported"):
        floor_time(START, "7m")


def test_candle_validates_shape_and_exposes_metrics() -> None:
    candle = candle_at(0)
    assert candle.range_bps == D("300")
    assert candle.body_bps == D("100")
    assert candle.duration == timedelta(hours=1)
    with pytest.raises(ValueError, match="high is below"):
        replace(candle, high=D("100.5"))
    with pytest.raises(ValueError, match="taker_buy_volume"):
        replace(candle, taker_buy_volume=D("3"))


def test_builder_rolls_closed_bar_and_tracks_trade_fields() -> None:
    builder = CandleBuilder("BTC/USDT:USDT", "1m")
    assert builder.on_trade(START, D("100"), D("2"), False) is None
    assert builder.on_trade(START + timedelta(seconds=30), D("110"), D("1"), True) is None

    forming = builder.forming()
    assert forming is not None and not forming.is_closed
    assert (forming.open, forming.high, forming.low, forming.close) == (
        D("100"),
        D("110"),
        D("100"),
        D("110"),
    )
    assert forming.volume == D("3")
    assert forming.quote_volume == D("310")
    assert forming.taker_buy_volume == D("2")
    assert forming.vwap == D("310") / D("3")

    closed = builder.on_trade(START + timedelta(minutes=2), D("120"), D("1"))
    assert closed is not None and closed.is_closed
    assert closed.open_time == START
    # No synthetic 00:01 candle is created; the new forming bar starts at 00:02.
    assert builder.forming() is not None
    assert builder.forming().open_time == START + timedelta(minutes=2)  # type: ignore[union-attr]


def test_builder_rejects_out_of_order_and_only_timer_closes_at_boundary() -> None:
    builder = CandleBuilder("BTCUSDT", "1m")
    builder.on_trade(START + timedelta(seconds=20), D("100"), D("1"))
    with pytest.raises(ValueError, match="ordered"):
        builder.on_trade(START + timedelta(seconds=10), D("99"), D("1"))
    assert builder.close_if_elapsed(START + timedelta(seconds=59)) is None
    assert builder.close_if_elapsed(START + timedelta(minutes=1)).is_closed  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="already-closed"):
        builder.on_trade(START + timedelta(seconds=40), D("101"), D("1"))


def test_merge_requires_four_consecutive_aligned_1h_candles() -> None:
    parts = [candle_at(i, close=str(101 + i)) for i in range(4)]
    merged = merge_candles("BTC/USDT:USDT", "4h", parts)
    assert merged.open_time == START
    assert merged.close_time == START + timedelta(hours=4)
    assert merged.open == D("100")
    assert merged.close == D("104")
    assert merged.volume == D("8")
    assert merged.trade_count == 8
    assert merged.taker_buy_volume == D("2")
    assert merged.vwap == D("804") / D("8")

    with pytest.raises(ValueError, match="exactly 4"):
        merge_candles("BTC/USDT:USDT", "4h", parts[:3])
    gapped = [parts[0], parts[1], parts[3], candle_at(4)]
    with pytest.raises(ValueError, match="target bucket|gap"):
        merge_candles("BTC/USDT:USDT", "4h", gapped)
    with pytest.raises(ValueError, match="closed"):
        merge_candles("BTC/USDT:USDT", "4h", [replace(parts[0], is_closed=False), *parts[1:]])


def test_streaming_aggregator_skips_incomplete_bucket() -> None:
    aggregator = CandleAggregator("BTC/USDT:USDT", "1h", "4h")
    assert aggregator.on_candle(candle_at(0)) is None
    assert aggregator.on_candle(candle_at(1)) is None
    # Hour 2 is missing. Reaching the bucket end does not invent it.
    assert aggregator.on_candle(candle_at(3)) is None
    for hour in range(4, 7):
        assert aggregator.on_candle(candle_at(hour)) is None
    assert aggregator.on_candle(candle_at(7)).timeframe == "4h"  # type: ignore[union-attr]


def test_offline_resample_matches_streaming_merge() -> None:
    parts = [candle_at(i) for i in range(8)]
    result = aggregate_candle_series("BTC/USDT:USDT", "1h", "4h", parts)
    assert result == (
        merge_candles("BTC/USDT:USDT", "4h", parts[:4]),
        merge_candles("BTC/USDT:USDT", "4h", parts[4:]),
    )


def test_pipeline_builds_exact_1m_to_1d_chain_deterministically() -> None:
    trades = [
        Trade(START + timedelta(minutes=minute), D(str(100 + minute)), D("1"), minute % 2 == 0)
        for minute in range(240)
    ]
    forward = build_candles_from_trades("BTCUSDT", trades, close_through=START + timedelta(hours=4))
    replay = build_candles_from_trades(
        "BTCUSDT", reversed(trades), close_through=START + timedelta(hours=4)
    )
    assert (
        hashlib.sha256(repr(forward).encode()).digest()
        == hashlib.sha256(repr(replay).encode()).digest()
    )
    output = replay
    assert {timeframe: len(rows) for timeframe, rows in output.items()} == {
        "1m": 240,
        "5m": 48,
        "15m": 16,
        "1h": 4,
        "4h": 1,
        "1d": 0,
    }
    four_hour = output["4h"][0]
    assert (four_hour.open, four_hour.close) == (D("100"), D("339"))
    assert four_hour.volume == D("240")
    assert four_hour.trade_count == 240
    assert all(candle.is_closed for rows in output.values() for candle in rows)


def test_three_complete_hours_never_publish_a_4h_candle() -> None:
    trades = [Trade(START + timedelta(minutes=minute), D("100"), D("1")) for minute in range(180)]
    output = build_candles_from_trades("BTCUSDT", trades, close_through=START + timedelta(hours=3))
    assert len(output["1h"]) == 3
    assert output["4h"] == ()


def test_six_complete_4h_candles_publish_one_utc_day() -> None:
    parts: list[Candle] = []
    for index in range(6):
        opened = START + timedelta(hours=index * 4)
        parts.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="4h",
                open_time=opened,
                close_time=opened + timedelta(hours=4),
                open=D("100"),
                high=D("110"),
                low=D("90"),
                close=D(str(101 + index)),
                volume=D("2"),
                quote_volume=D("202"),
                trade_count=2,
                taker_buy_volume=D("1"),
                vwap=D("101"),
            )
        )
    daily = merge_candles("BTCUSDT", "1d", parts)
    assert daily.open_time == START
    assert daily.close_time == START + timedelta(days=1)
    assert daily.volume == D("12")
    assert daily.vwap == D("1212") / D("12")


def test_pipeline_never_publishes_forming_bar() -> None:
    seen: list[Candle] = []
    pipeline = CandlePipeline("BTCUSDT", subscribers=(seen.append,))
    assert pipeline.on_trade(START, D("100"), D("1")) == ()
    assert seen == []
    assert pipeline.forming() is not None and not pipeline.forming().is_closed  # type: ignore[union-attr]
    published = pipeline.advance_time(START + timedelta(minutes=1))
    assert published[0].is_closed
    assert seen == [published[0]]
    # Advancing across further quiet minutes does not invent empty OHLC bars.
    assert pipeline.advance_time(START + timedelta(minutes=5)) == ()
    assert seen == [published[0]]


def test_pipeline_delivers_closed_event_before_durable_sink() -> None:
    seen: list[Candle] = []

    class BrokenStore:
        def read(self, _symbol, _timeframe):
            return []

        def upsert(self, _candles):
            raise OSError("durable sink unavailable")

    pipeline = CandlePipeline(
        "BTCUSDT",
        subscribers=(seen.append,),
        store=BrokenStore(),  # type: ignore[arg-type]
    )
    pipeline.on_trade(START, D("100"), D("1"))

    with pytest.raises(OSError, match="durable sink unavailable"):
        pipeline.advance_time(START + timedelta(minutes=1))

    assert len(seen) == 1
    assert seen[0].is_closed is True
    assert pipeline.persistence_healthy is False
    assert pipeline.last_persistence_error == "OSError: durable sink unavailable"


def test_failed_subscriber_isolated_and_durable_store_still_advances(tmp_path) -> None:
    store = CandleParquetStore(tmp_path, exchange="binanceusdm")
    seen: list[Candle] = []

    def broken(_candle: Candle) -> None:
        raise RuntimeError("lane consumer failed")

    pipeline = CandlePipeline(
        "BTC/USDT:USDT",
        subscribers=(broken, seen.append),
        store=store,
    )
    pipeline.on_trade(START, D("100"), D("1"))

    pipeline.advance_time(START + timedelta(minutes=1))

    assert pipeline.subscriber_failures == 1
    assert pipeline.persistence_healthy is True
    assert seen == store.read("BTCUSDT", "1m")


def test_pipeline_reports_publish_and_persist_clocks_without_changing_delivery() -> None:
    timings: list[tuple[str, float]] = []

    class Store:
        def read(self, _symbol, _timeframe):
            return []

        def upsert(self, _candles):
            return None

    pipeline = CandlePipeline(
        "BTCUSDT",
        store=Store(),  # type: ignore[arg-type]
        timing_sink=lambda name, ms: timings.append((name, ms)),
    )
    for minute in range(6):
        pipeline.on_trade(START + timedelta(minutes=minute), D("100"), D("1"))

    names = [name for name, _ in timings]
    assert names.count("base_close_publish_ms") == 5
    assert "aggregate_publish_ms" in names
    assert names.count("parquet_persist_ms") == 6
    assert all(ms >= 0 for _, ms in timings)


def test_pipeline_restart_repairs_higher_bars_from_persisted_base(tmp_path) -> None:
    trades = [
        Trade(START + timedelta(minutes=minute), D(str(100 + minute)), D("1"))
        for minute in range(60)
    ]
    base = build_candles_from_trades(
        "BTCUSDT",
        trades,
        close_through=START + timedelta(hours=1),
    )["1m"]
    store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    store.upsert(base)
    assert store.read("BTCUSDT", "1h") == []

    CandlePipeline("BTCUSDT", store=store)

    hours = store.read("BTCUSDT", "1h")
    assert len(hours) == 1
    assert hours[0].open_time == START
    assert hours[0].close_time == START + timedelta(hours=1)
    assert hours[0].trade_count == 60


def test_store_reads_exact_canonical_bar_from_its_partition(tmp_path) -> None:
    store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    expected = candle_at(0)
    store.upsert((expected,))

    assert store.read_at(expected.symbol, "1h", expected.open_time) == expected
    assert store.read_at(
        expected.symbol, "1h", expected.open_time + timedelta(hours=1)
    ) is None


def test_pipeline_restart_repairs_interior_holes_without_filling_source_gap(tmp_path) -> None:
    trades = [
        Trade(START + timedelta(minutes=minute), D(str(100 + minute)), D("1"))
        for minute in range(120)
    ]
    output = build_candles_from_trades(
        "BTCUSDT",
        trades,
        close_through=START + timedelta(hours=2),
    )
    store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    # Simulate a real source hole plus a newer child bar left behind by a
    # restart. Recovery may repair complete buckets around the hole, but must
    # never synthesize the missing 00:30 minute or its parent buckets.
    base = [candle for candle in output["1m"] if candle.open_time != START + timedelta(minutes=30)]
    store.upsert(base)
    store.upsert((output["5m"][-1],))

    CandlePipeline("BTCUSDT", store=store)

    five_minute_opens = {candle.open_time for candle in store.read("BTCUSDT", "5m")}
    assert START + timedelta(minutes=30) not in five_minute_opens
    assert START + timedelta(minutes=25) in five_minute_opens
    assert START + timedelta(minutes=35) in five_minute_opens
    hours = store.read("BTCUSDT", "1h")
    assert [candle.open_time for candle in hours] == [START + timedelta(hours=1)]


def test_late_trade_is_logged_quarantined_and_cannot_rewrite_closed_parquet(
    tmp_path, caplog
) -> None:
    store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    quarantine_path = tmp_path / "forensics/rejected_trades.jsonl"
    pipeline = CandlePipeline(
        "BTCUSDT",
        store=store,
        rejected_trade_sink=JsonlTradeQuarantine(quarantine_path),
    )
    pipeline.on_trade(START, D("100"), D("1"))
    closed = pipeline.advance_time(START + timedelta(minutes=1))[0]
    parquet_path = store.partition_path(closed)
    before = hashlib.sha256(parquet_path.read_bytes()).digest()

    with caplog.at_level(logging.WARNING), pytest.raises(ValueError, match="already-closed"):
        pipeline.on_trade(START + timedelta(seconds=30), D("999"), D("50"))

    assert hashlib.sha256(parquet_path.read_bytes()).digest() == before
    assert store.read("BTCUSDT", "1m") == [closed]
    assert "candle trade rejected" in caplog.text
    records = [json.loads(line) for line in quarantine_path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["price"] == "999"
    assert "already-closed" in records[0]["reason"]


def test_research_only_1s_base_rolls_into_1m() -> None:
    trades = [
        Trade(START + timedelta(seconds=second), D(str(100 + second)), D("1"))
        for second in range(60)
    ]
    output = build_candles_from_trades(
        "BTCUSDT",
        trades,
        base_timeframe="1s",
        close_through=START + timedelta(minutes=1),
    )
    assert len(output["1s"]) == 60
    assert len(output["1m"]) == 1
    assert output["1m"][0].trade_count == 60
    assert output["1m"][0].close == D("159")


def test_tick_lake_frame_conversion_is_stable_and_maps_aggressor_side() -> None:
    frame = pd.DataFrame(
        [
            {"ts_ms": 2_000, "price": 102.0, "amount": 2.0, "side": "sell"},
            {"ts_ms": 1_000, "price": 101.0, "amount": 1.0, "side": "buy"},
            {"ts_ms": 3_000, "price": 103.0, "amount": 3.0, "side": ""},
        ]
    )
    trades = trades_from_tick_frame(frame)
    assert [trade.timestamp for trade in trades] == [
        datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC),
        datetime(1970, 1, 1, 0, 0, 2, tzinfo=UTC),
        datetime(1970, 1, 1, 0, 0, 3, tzinfo=UTC),
    ]
    assert [trade.is_buyer_maker for trade in trades] == [False, True, None]
    assert trades[0].price == D("101.0")

    with pytest.raises(ValueError, match="missing required"):
        trades_from_tick_frame(frame.drop(columns="amount"))


def test_parquet_store_partitions_round_trips_and_upserts(tmp_path) -> None:
    store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    one_hour = candle_at(0)
    result = store.upsert((one_hour,))
    assert result.rows_written == 1
    assert result.paths == (tmp_path / "candles/exchange=binanceusdm/BTCUSDT/1h/2026-08.parquet",)
    updated = replace(one_hour, close=D("100.5"), quote_volume=D("202"), vwap=D("101"))
    store.upsert((updated,))
    rows = store.read("BTC/USDT:USDT", "1h")
    assert rows == [updated]
    assert len(rows) == 1  # duplicate open_time is an idempotent replacement

    forming = replace(one_hour, is_closed=False)
    with pytest.raises(ValueError, match="forming"):
        store.upsert((forming,))


def test_parquet_partition_lock_preserves_concurrent_writers(tmp_path) -> None:
    store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    candles = [candle_at(hour) for hour in range(12)]

    def write(candle: Candle) -> None:
        store.upsert((candle,))

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, candles))

    rows = store.read("BTC/USDT:USDT", "1h")
    assert [row.open_time for row in rows] == [row.open_time for row in candles]
    assert len(rows) == len(candles)
    assert store.partition_path(candles[0]).with_suffix(".parquet.lock").exists()


def test_storage_decimal_handles_ordinary_quote_volumes() -> None:
    """Values from 1e10 up crashed the writer: quantize needs prec > 28.

    An hour of BTC quote volume is tens of billions, so this was not an edge
    case -- it took the recorder down mid-flush with a bare InvalidOperation.
    """
    from decimal import Decimal

    from vnedge.data.candles import CandleParquetStore

    for raw in ("12345678901.5", "1234567890123.45", "99999999999999999.123456789"):
        stored = CandleParquetStore._storage_decimal(Decimal(raw))
        assert stored == Decimal(raw), raw
        assert -stored.as_tuple().exponent == 18, raw
    assert CandleParquetStore._storage_decimal(None) is None


def test_storage_decimal_rejects_values_the_column_cannot_hold() -> None:
    """Out-of-range must name the value, not surface as a bare InvalidOperation."""
    from decimal import Decimal

    import pytest

    from vnedge.data.candles import CandleParquetStore

    too_big = Decimal("1" + "0" * 30)  # 31 int digits + 18 scale > 38
    with pytest.raises(ValueError, match="does not fit decimal128"):
        CandleParquetStore._storage_decimal(too_big)
