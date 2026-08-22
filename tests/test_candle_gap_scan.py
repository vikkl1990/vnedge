"""Tests for the candle-sequence gap scanner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from vnedge.data.candle_gap_scan import find_candle_holes
from vnedge.data.candles import Candle
from vnedge.data.gaps import GapKind

BASE = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
DETECTED = datetime(2026, 8, 19, 3, 0, tzinfo=UTC)


def _candle(offset_hours: int) -> Candle:
    open_time = BASE + timedelta(hours=offset_hours)
    return Candle(
        symbol="BTCUSDT", timeframe="1h", open_time=open_time,
        close_time=open_time + timedelta(hours=1),
        open=Decimal(100), high=Decimal(101), low=Decimal(99),
        close=Decimal("100.5"), volume=Decimal(10),
        quote_volume=Decimal(1005), trade_count=5,
        taker_buy_volume=Decimal(5),
    )


def test_contiguous_sequence_reports_nothing() -> None:
    candles = [_candle(k) for k in range(6)]
    assert find_candle_holes(candles, exchange="binanceusdm", symbol="BTCUSDT",
                             detected_at=DETECTED) == []


def test_a_missing_hour_is_recorded_as_a_storage_hole() -> None:
    # 12:00, 13:00, [15:00 missing], 16:00 -- the shape found live on 17 Aug
    candles = [_candle(0), _candle(1), _candle(3), _candle(4)]
    holes = find_candle_holes(candles, exchange="binanceusdm", symbol="BTCUSDT",
                              detected_at=DETECTED)
    assert len(holes) == 1
    hole = holes[0]
    assert hole.kind is GapKind.STORAGE_HOLE
    assert hole.start == BASE + timedelta(hours=2)   # close of the 13:00 bar
    assert hole.end == BASE + timedelta(hours=3)     # open of the 15:00 bar
    assert "60 min missing" in hole.detail


def test_same_storage_hole_has_stable_id_across_periodic_scans() -> None:
    candles = [_candle(0), _candle(2)]
    first = find_candle_holes(
        candles,
        exchange="binanceusdm",
        symbol="BTCUSDT",
        detected_at=DETECTED,
    )[0]
    second = find_candle_holes(
        candles,
        exchange="binanceusdm",
        symbol="BTCUSDT",
        detected_at=DETECTED + timedelta(minutes=15),
    )[0]

    assert first.gap_id == second.gap_id


def test_multiple_holes_are_reported_separately() -> None:
    candles = [_candle(0), _candle(2), _candle(3), _candle(7)]
    holes = find_candle_holes(candles, exchange="binanceusdm", symbol="BTCUSDT",
                              detected_at=DETECTED)
    assert len(holes) == 2
    spans = [(h.end - h.start).total_seconds() / 60 for h in holes]
    assert spans == [60.0, 180.0]


def test_scanner_does_not_invent_a_hole_before_the_first_bar() -> None:
    """The first bar has no predecessor; absence of history is not a gap."""
    assert find_candle_holes([_candle(5)], exchange="binanceusdm",
                             symbol="BTCUSDT", detected_at=DETECTED) == []
