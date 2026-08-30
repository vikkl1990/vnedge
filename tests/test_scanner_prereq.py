import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from vnedge.data.candles import Candle, CandleParquetStore
from vnedge.data.scanner_prereq import (
    DEFAULT_REQUIREMENTS,
    requirements_from_roster,
    scanner_prerequisites,
)


def test_default_requirements_cover_active_scanner_warmups() -> None:
    assert DEFAULT_REQUIREMENTS == {
        "5m": 2066,
        "15m": 2018,
        "1h": 24,
        "4h": 12,
    }


def test_roster_requirements_follow_active_strategy_dependencies(tmp_path) -> None:
    roster = tmp_path / "roster.json"
    roster.write_text(
        json.dumps(
            {
                "version": 1,
                "observers": [
                    {
                        "strategy_id": "session_continuation_15m_v1",
                        "exchange": "binanceusdm",
                        "symbols": ["BTC/USDT:USDT"],
                        "timeframe": "15m",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    # The scanner's local 33-bar feature window is not its full dependency:
    # the shared causal regime router first becomes finite after 111 bars.
    assert requirements_from_roster(roster) == {"1h": 24, "4h": 12, "15m": 112}


def _candles(symbol: str, timeframe: str, count: int, close: datetime) -> list[Candle]:
    step = timedelta(minutes=5 if timeframe == "5m" else 15)
    if timeframe == "1h":
        step = timedelta(hours=1)
    elif timeframe == "4h":
        step = timedelta(hours=4)
    start = close - count * step
    return [
        Candle(
            symbol=symbol,
            timeframe=timeframe,
            open_time=start + index * step,
            close_time=start + (index + 1) * step,
            open=Decimal(100),
            high=Decimal(101),
            low=Decimal(99),
            close=Decimal(100),
            volume=Decimal(2),
            quote_volume=Decimal(200),
            trade_count=2,
            taker_buy_volume=Decimal(1),
            vwap=Decimal(100),
        )
        for index in range(count)
    ]


def test_scanner_prerequisites_require_complete_exact_ladder(tmp_path):
    now = datetime(2026, 8, 22, 12, 3, tzinfo=UTC)
    store = CandleParquetStore(tmp_path, exchange="binanceusdm")
    requirements = {"5m": 3, "15m": 2, "1h": 2, "4h": 2}
    close = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    for timeframe, count in requirements.items():
        store.upsert(_candles("BTCUSDT", timeframe, count, close))

    report = scanner_prerequisites(
        tmp_path,
        exchange="binanceusdm",
        symbols=["BTC/USDT:USDT"],
        requirements=requirements,
        now=now,
    )

    assert report.ready is True
    assert {row.reason for row in report.rows} == {"ok"}


def test_scanner_prerequisites_fail_closed_on_stale_or_inexact_tail(tmp_path):
    now = datetime(2026, 8, 22, 12, 3, tzinfo=UTC)
    store = CandleParquetStore(tmp_path, exchange="binanceusdm")
    stale = _candles(
        "BTCUSDT", "5m", 3, datetime(2026, 8, 22, 11, 55, tzinfo=UTC)
    )
    store.upsert(stale)

    report = scanner_prerequisites(
        tmp_path,
        exchange="binanceusdm",
        symbols=["BTC/USDT:USDT"],
        requirements={"5m": 3},
        now=now,
    )

    assert report.ready is False
    assert report.rows[0].reason == "stale_tail"
    assert report.rows[0].issues == ("stale_tail",)
    assert report.rows[0].lag_seconds == 300
    assert report.rows[0].missing_bars == 0


def test_scanner_prerequisites_fail_closed_on_non_exact_volume(tmp_path):
    now = datetime(2026, 8, 22, 12, 3, tzinfo=UTC)
    candles = _candles(
        "BTCUSDT", "5m", 3, datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    )
    invalid = candles[-1]
    candles[-1] = Candle(
        symbol=invalid.symbol,
        timeframe=invalid.timeframe,
        open_time=invalid.open_time,
        close_time=invalid.close_time,
        open=invalid.open,
        high=invalid.high,
        low=invalid.low,
        close=invalid.close,
        volume=invalid.volume,
        quote_volume=Decimal(0),
        trade_count=invalid.trade_count,
        taker_buy_volume=invalid.taker_buy_volume,
        vwap=None,
    )
    CandleParquetStore(tmp_path, exchange="binanceusdm").upsert(candles)

    report = scanner_prerequisites(
        tmp_path,
        exchange="binanceusdm",
        symbols=["BTC/USDT:USDT"],
        requirements={"5m": 3},
        now=now,
    )

    assert report.ready is False
    assert report.rows[0].reason == "non_exact_volume"
    assert report.rows[0].invalid_exact_volume_bars == 1


def test_scanner_prerequisites_report_exact_gap_diagnostics(tmp_path):
    now = datetime(2026, 8, 22, 12, 3, tzinfo=UTC)
    candles = _candles(
        "BTCUSDT", "5m", 4, datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    )
    # Remove 11:50 while retaining a current 12:00 close.
    del candles[-2]
    CandleParquetStore(tmp_path, exchange="binanceusdm").upsert(candles)

    report = scanner_prerequisites(
        tmp_path,
        exchange="binanceusdm",
        symbols=["BTC/USDT:USDT"],
        requirements={"5m": 3},
        now=now,
    )

    row = report.rows[0]
    assert report.ready is False
    assert row.reason == "non_contiguous"
    assert row.gap_count == 1
    assert row.first_gap_open == "2026-08-22T11:50:00+00:00"
