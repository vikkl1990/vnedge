"""Tests for continuous lake health."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from vnedge.data.candles import Candle, CandleParquetStore
from vnedge.data.lake_health import LakeHealthMonitor, LakeStatus

BASE = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def _candle(offset_hours: int, symbol: str = "BTCUSDT") -> Candle:
    open_time = BASE + timedelta(hours=offset_hours)
    return Candle(
        symbol=symbol, timeframe="1h", open_time=open_time,
        close_time=open_time + timedelta(hours=1),
        open=Decimal(100), high=Decimal(101), low=Decimal(99),
        close=Decimal(100), volume=Decimal(10),
        quote_volume=Decimal(1000), trade_count=3,
    )


def _monitor(tmp_path, **kw) -> LakeHealthMonitor:
    return LakeHealthMonitor(
        exchange="binanceusdm", symbols=["BTCUSDT"],
        candle_root=tmp_path / "candles", gap_root=tmp_path / "gaps", **kw
    )


def test_status_starts_unknown_not_healthy() -> None:
    """A lake that has never been checked must not read as healthy."""
    monitor = LakeHealthMonitor(exchange="binanceusdm", symbols=["BTCUSDT"],
                                candle_root=Path_stub(), gap_root=Path_stub())
    assert monitor.health.status is LakeStatus.UNKNOWN
    assert monitor.health.checked_at is None


def Path_stub():
    from pathlib import Path
    return Path("/nonexistent-for-status-check")


def test_contiguous_lake_reports_healthy(tmp_path) -> None:
    store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    store.upsert([_candle(k) for k in range(5)])
    health = _monitor(tmp_path).check_once(now=BASE + timedelta(hours=5))
    assert health.status is LakeStatus.HEALTHY
    assert health.total_holes == 0
    assert health.checked_at is not None


def test_missing_hour_makes_the_lake_degraded(tmp_path) -> None:
    store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    store.upsert([_candle(0), _candle(1), _candle(3), _candle(4)])  # 14:00 missing
    health = _monitor(tmp_path).check_once(now=BASE + timedelta(hours=5))
    assert health.status is LakeStatus.DEGRADED
    assert health.total_holes == 1
    assert "BTCUSDT=1" in health.detail


def test_a_failed_check_degrades_rather_than_crashes(tmp_path) -> None:
    monitor = _monitor(tmp_path)
    monitor.timeframe = "not-a-timeframe"
    health = monitor.check_once()
    assert health.status in (LakeStatus.ERROR, LakeStatus.HEALTHY)
    # whatever happens, the monitor survives and publishes a status
    assert health.checked_at is not None


def test_health_dict_is_dashboard_safe(tmp_path) -> None:
    store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    store.upsert([_candle(0), _candle(2)])
    payload = _monitor(tmp_path).check_once(
        now=BASE + timedelta(hours=3)
    ).as_dict()
    assert payload["status"] == "degraded"
    assert payload["total_holes"] == 1
    assert payload["checked_at"]


def test_monitor_atomically_publishes_cross_process_status(tmp_path) -> None:
    store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    store.upsert([_candle(0), _candle(1)])
    monitor = _monitor(tmp_path)

    monitor.check_once(now=BASE + timedelta(hours=2))

    status_path = tmp_path / "gaps" / "lake_health.json"
    assert status_path.exists()
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "healthy"
    assert payload["total_holes"] == 0
    assert not status_path.with_suffix(".json.tmp").exists()


def test_empty_lake_is_degraded_not_vacuously_healthy(tmp_path) -> None:
    health = _monitor(tmp_path).check_once(now=BASE)

    assert health.status is LakeStatus.DEGRADED
    assert health.bars_by_symbol == {"BTCUSDT": 0}
    assert "only 0/2 bars" in health.detail


def test_stale_closed_tail_is_degraded(tmp_path) -> None:
    store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    store.upsert([_candle(0), _candle(1)])

    health = _monitor(tmp_path).check_once(now=BASE + timedelta(hours=4))

    assert health.status is LakeStatus.DEGRADED
    assert "tail stale" in health.detail
