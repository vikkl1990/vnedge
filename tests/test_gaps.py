from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from vnedge.data.candles import Candle, CandleParquetStore
from vnedge.data.gaps import (
    GapAwareCandlePipeline,
    GapKind,
    GapParquetStore,
    GapRecord,
    IdentifiedTrade,
    StreamIntegrityGuard,
    candles_without_gaps,
    coverage_fraction,
    merge_identified_trades,
    offline_trade_time_holes,
    storage_holes_from_days,
)

D = Decimal
START = datetime(2026, 8, 16, tzinfo=UTC)


def trade(
    trade_id: str,
    seconds: int,
    *,
    price: str = "100",
    amount: str = "1",
) -> IdentifiedTrade:
    return IdentifiedTrade(
        trade_id,
        START + timedelta(seconds=seconds),
        D(price),
        D(amount),
    )


def candle(hour: int) -> Candle:
    opened = START + timedelta(hours=hour)
    return Candle(
        symbol="BTCUSDT",
        timeframe="1h",
        open_time=opened,
        close_time=opened + timedelta(hours=1),
        open=D("100"),
        high=D("101"),
        low=D("99"),
        close=D("100"),
        volume=D("1"),
        quote_volume=D("100"),
        trade_count=1,
        vwap=D("100"),
    )


def test_gap_record_and_store_are_utc_idempotent_and_recoverable(tmp_path) -> None:
    store = GapParquetStore(tmp_path / "gaps")
    record = GapRecord(
        "BTC/USDT:USDT",
        "binanceusdm",
        GapKind.STREAM_STALE,
        START,
        START + timedelta(seconds=20),
        START + timedelta(seconds=20),
        "websocket silent",
    )
    store.upsert((record,))
    recovered = replace(record, end=START + timedelta(seconds=30), recovered=True)
    store.upsert((recovered,))
    assert store.read("binanceusdm", "BTC/USDT:USDT") == [recovered]
    assert store.partition_path(record).exists()

    with pytest.raises(ValueError, match="timezone-aware"):
        GapRecord(
            "BTCUSDT",
            "binanceusdm",
            GapKind.STREAM_STALE,
            datetime(2026, 8, 16),  # noqa: DTZ001 - intentional boundary test
            START,
            START,
        )


def test_healthy_heartbeats_make_quiet_market_not_a_gap() -> None:
    guard = StreamIntegrityGuard(
        "binanceusdm",
        "BTCUSDT",
        stale_after=timedelta(seconds=10),
        monitoring_started_at=START,
    )
    assert guard.entries_blocked  # not warm before the first healthy message
    assert guard.on_message(START + timedelta(seconds=9))
    assert not guard.entries_blocked
    assert not guard.check_stale(START + timedelta(seconds=18))
    assert guard.active_gaps == ()


def test_late_arriving_message_cannot_hide_a_stale_interval() -> None:
    guard = StreamIntegrityGuard(
        "binanceusdm",
        "BTCUSDT",
        stale_after=timedelta(seconds=10),
        monitoring_started_at=START,
    )
    assert not guard.on_message(START + timedelta(seconds=11))
    assert guard.entries_blocked and guard.data_degraded
    assert {record.kind for record in guard.active_gaps} == {GapKind.STREAM_STALE}


def test_stale_stream_freezes_close_until_backfill_and_warm_message(tmp_path) -> None:
    candle_store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    gap_store = GapParquetStore(tmp_path / "gaps")
    pipeline = GapAwareCandlePipeline(
        "binanceusdm",
        "BTCUSDT",
        monitoring_started_at=START,
        stale_after=timedelta(seconds=10),
        candle_store=candle_store,
        gap_store=gap_store,
    )
    pipeline.on_trade(trade("t1", 0), received_at=START, sequence_id=1)
    assert pipeline.advance_time(START + timedelta(seconds=20)) == ()
    assert pipeline.data_degraded and pipeline.entries_blocked
    assert pipeline.exits_allowed
    assert pipeline.pipeline.forming() is not None

    assert pipeline.recover(
        (trade("t1", 0), trade("t2", 15, price="110")),
        at=START + timedelta(seconds=20),
        continuity_proven=True,
        detail="REST overlap matched",
    )
    forming = pipeline.pipeline.forming()
    assert forming is not None
    assert forming.trade_count == 2
    assert forming.close == D("110")
    assert pipeline.entries_blocked  # continuity proven, but feed not warm yet

    pipeline.on_heartbeat(START + timedelta(seconds=21))
    assert not pipeline.entries_blocked
    pipeline.on_heartbeat(START + timedelta(seconds=30))
    pipeline.on_heartbeat(START + timedelta(seconds=40))
    pipeline.on_heartbeat(START + timedelta(seconds=50))
    pipeline.on_heartbeat(START + timedelta(seconds=59))
    closed = pipeline.advance_time(START + timedelta(minutes=1))
    assert len(closed) == 1 and closed[0].is_closed
    records = gap_store.read("binanceusdm", "BTCUSDT")
    assert len(records) == 1 and records[0].recovered


def test_sequence_break_blocks_entries_and_withholds_trade() -> None:
    pipeline = GapAwareCandlePipeline(
        "bybit",
        "BTCUSDT",
        monitoring_started_at=START,
    )
    assert pipeline.on_trade(trade("t1", 0), received_at=START, sequence_id=100) == ()
    assert (
        pipeline.on_trade(
            trade("t2", 1),
            received_at=START + timedelta(seconds=1),
            sequence_id=102,
        )
        == ()
    )
    assert pipeline.data_degraded and pipeline.entries_blocked
    assert {record.kind for record in pipeline.guard.active_gaps} == {GapKind.SEQ_BREAK}
    assert pipeline.pipeline.forming().trade_count == 1  # type: ignore[union-attr]


def test_failed_backfill_remains_blocked_and_is_audited(tmp_path) -> None:
    store = GapParquetStore(tmp_path / "gaps")
    guard = StreamIntegrityGuard(
        "delta_india",
        "BTCUSD",
        stale_after=timedelta(seconds=5),
        monitoring_started_at=START,
        store=store,
    )
    guard.on_message(START)
    assert guard.check_stale(START + timedelta(seconds=6))
    assert not guard.recover(
        START + timedelta(seconds=10),
        continuity_proven=False,
        detail="REST unavailable",
    )
    assert guard.entries_blocked and guard.data_degraded
    assert {record.kind for record in store.read("delta_india", "BTCUSD")} == {
        GapKind.STREAM_STALE,
        GapKind.BACKFILL_FAIL,
    }


def test_late_trade_creates_gap_and_never_rewrites_closed_candle(tmp_path) -> None:
    candle_store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    gap_store = GapParquetStore(tmp_path / "gaps")
    pipeline = GapAwareCandlePipeline(
        "binanceusdm",
        "BTCUSDT",
        monitoring_started_at=START,
        stale_after=timedelta(seconds=60),
        candle_store=candle_store,
        gap_store=gap_store,
    )
    pipeline.on_trade(trade("t1", 0), received_at=START, sequence_id=1)
    pipeline.on_heartbeat(START + timedelta(seconds=59), sequence_id=2)
    closed = pipeline.advance_time(START + timedelta(minutes=1))[0]
    path = candle_store.partition_path(closed)
    before = hashlib.sha256(path.read_bytes()).digest()

    with pytest.raises(ValueError, match="already-closed"):
        pipeline.on_trade(
            trade("late", 30, price="999"),
            received_at=START + timedelta(minutes=1, seconds=1),
            sequence_id=3,
        )

    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert pipeline.data_degraded and pipeline.entries_blocked
    assert GapKind.LATE_TRADE in {record.kind for record in pipeline.guard.active_gaps}


def test_overlap_backfill_merge_is_idempotent_and_rejects_id_conflicts() -> None:
    first = trade("same", 1)
    second = trade("next", 2)
    assert merge_identified_trades((first,), (first, second)) == (first, second)
    with pytest.raises(ValueError, match="conflicting payload"):
        merge_identified_trades((first,), (replace(first, price=D("101")),))


def test_backfill_window_uses_last_event_overlap() -> None:
    guard = StreamIntegrityGuard(
        "binanceusdm",
        "BTCUSDT",
        stale_after=timedelta(seconds=10),
        monitoring_started_at=START,
    )
    guard.on_message(
        START + timedelta(seconds=5),
        event_time=START + timedelta(seconds=4),
    )
    assert guard.backfill_window(START + timedelta(minutes=5), overlap=timedelta(minutes=1)) == (
        START - timedelta(seconds=56),
        START + timedelta(minutes=5),
    )


def test_research_gap_filter_and_coverage_do_not_forward_fill() -> None:
    bars = tuple(candle(hour) for hour in range(4))
    gaps = (
        GapRecord(
            "BTCUSDT",
            "binanceusdm",
            GapKind.STORAGE_HOLE,
            START + timedelta(hours=1),
            START + timedelta(hours=2),
            START + timedelta(hours=4),
        ),
        GapRecord(
            "BTCUSDT",
            "binanceusdm",
            GapKind.STREAM_STALE,
            START + timedelta(hours=1, minutes=30),
            START + timedelta(hours=3),
            START + timedelta(hours=4),
        ),
    )
    assert candles_without_gaps(bars, gaps) == (bars[0], bars[3])
    assert coverage_fraction(START, START + timedelta(hours=4), gaps) == D("0.5")


def test_recovered_gaps_do_not_remove_rebuilt_research_candles() -> None:
    bars = tuple(candle(hour) for hour in range(4))
    recovered = GapRecord(
        "BTCUSDT",
        "binanceusdm",
        GapKind.STORAGE_HOLE,
        START + timedelta(hours=1),
        START + timedelta(hours=3),
        START + timedelta(hours=4),
        recovered=True,
    )

    assert candles_without_gaps(bars, (recovered,)) == bars
    assert coverage_fraction(START, START + timedelta(hours=4), (recovered,)) == D("1")


def test_storage_inventory_emits_explicit_day_holes() -> None:
    holes = storage_holes_from_days(
        "binanceusdm",
        "BTCUSDT",
        (date(2026, 8, 15), date(2026, 8, 16), date(2026, 8, 17)),
        (date(2026, 8, 15), date(2026, 8, 17)),
        detected_at=START + timedelta(days=2),
    )
    assert len(holes) == 1
    assert holes[0].kind == GapKind.STORAGE_HOLE
    assert holes[0].start == START
    assert holes[0].end == START + timedelta(days=1)


def test_offline_trade_intervals_require_explicit_coverage_expectation() -> None:
    sparse = (trade("t2", 120), trade("t1", 0))
    common = {
        "max_expected_gap": timedelta(seconds=30),
        "detected_at": START + timedelta(minutes=3),
        "expected_start": START,
        "expected_end": START + timedelta(minutes=3),
    }
    assert (
        offline_trade_time_holes(
            "binanceusdm",
            "BTCUSDT",
            sparse,
            continuous_coverage_expected=False,
            **common,
        )
        == ()
    )

    holes = offline_trade_time_holes(
        "binanceusdm",
        "BTCUSDT",
        sparse,
        continuous_coverage_expected=True,
        **common,
    )
    assert [(hole.start, hole.end) for hole in holes] == [
        (START, START + timedelta(minutes=2)),
        (START + timedelta(minutes=2), START + timedelta(minutes=3)),
    ]
