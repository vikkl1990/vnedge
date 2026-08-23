from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pandas as pd
import pytest

from vnedge.data.binance_gap_recovery import (
    BinanceAggTradeRest,
    FetchedTape,
    _write_tape,
    recover_storage_gaps,
)
from vnedge.data.candles import Candle, CandleParquetStore
from vnedge.data.gaps import GapKind, GapParquetStore, GapRecord

START = datetime(2026, 8, 16, tzinfo=UTC)


def _trade(agg_id: int, timestamp: int, *, maker: bool = False) -> dict:
    return {
        "a": agg_id,
        "T": timestamp,
        "p": "100.25",
        "q": "0.5",
        "m": maker,
    }


def test_rest_fetch_pages_and_preserves_aggressor_side() -> None:
    start_ms = int(START.timestamp() * 1_000)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        calls.append(dict(params))
        from_id = params.get("fromId")
        if from_id is None:
            rows = [_trade(10, start_ms, maker=True), _trade(11, start_ms + 1)]
        elif from_id == "12":
            rows = [_trade(12, start_ms + 2)]
        else:
            rows = []
        return httpx.Response(200, json=rows)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = BinanceAggTradeRest(
        client=client,
        page_size=2,
        request_interval_seconds=0,
    )
    tape = fetcher.fetch("BTC/USDT:USDT", START, START + timedelta(minutes=1))

    assert tape.first_agg_id == 10 and tape.last_agg_id == 12
    assert tape.requests == 2 and tape.trades == 3
    assert list(tape.frame.columns) == ["ts_ms", "price", "amount", "side"]
    assert list(tape.frame["side"]) == ["sell", "buy", "buy"]
    assert calls[1]["fromId"] == "12"


def test_rest_fetch_rejects_an_aggregate_id_hole() -> None:
    start_ms = int(START.timestamp() * 1_000)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[_trade(10, start_ms), _trade(12, start_ms + 1)],
        )

    fetcher = BinanceAggTradeRest(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )
    with pytest.raises(ValueError, match="ID discontinuity"):
        fetcher.fetch("BTCUSDT", START, START + timedelta(minutes=1))


def test_gap_shard_is_atomic_and_idempotent(tmp_path) -> None:
    start_ms = int(START.timestamp() * 1_000)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_trade(10, start_ms)])

    fetcher = BinanceAggTradeRest(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )
    tape = fetcher.fetch("BTCUSDT", START, START + timedelta(minutes=1))
    first = _write_tape(tape, tmp_path, exchange="binanceusdm")
    second = _write_tape(tape, tmp_path, exchange="binanceusdm")

    assert first == second and len(first) == 1
    assert not list(first[0].parent.glob(".*.tmp"))
    stored = pd.read_parquet(first[0])
    assert list(stored.columns) == ["ts_ms", "price", "amount", "side"]
    assert stored.to_dict("records") == tape.frame.to_dict("records")


def test_existing_canonical_coverage_closes_duplicate_gap_records(tmp_path) -> None:
    candle_store = CandleParquetStore(
        tmp_path / "candles",
        exchange="binanceusdm",
    )
    candle_store.upsert(
        (
            Candle(
                symbol="BTCUSDT",
                timeframe="1h",
                open_time=START,
                close_time=START + timedelta(hours=1),
                open=Decimal(100),
                high=Decimal(101),
                low=Decimal(99),
                close=Decimal(100),
                volume=Decimal(1),
                quote_volume=Decimal(100),
                trade_count=1,
            ),
        )
    )
    gap_store = GapParquetStore(tmp_path / "gaps")
    records = tuple(
        GapRecord(
            symbol="BTCUSDT",
            exchange="binanceusdm",
            kind=GapKind.STORAGE_HOLE,
            start=START,
            end=START + timedelta(hours=1),
            detected_at=START + timedelta(hours=offset),
            gap_id=f"duplicate-{offset}",
        )
        for offset in (2, 3)
    )
    gap_store.upsert(records)

    class MustNotFetch:
        def fetch(self, *_args, **_kwargs):
            raise AssertionError("covered canonical interval must not refetch")

    report = recover_storage_gaps(
        data_root=tmp_path,
        candle_root=tmp_path / "candles",
        gap_root=tmp_path / "gaps",
        exchange="binanceusdm",
        symbols=["BTCUSDT"],
        fetcher=MustNotFetch(),
    )

    assert not report.recovered
    stored_records = gap_store.read("binanceusdm", "BTCUSDT")
    assert len(stored_records) == 2
    assert all(record.recovered for record in stored_records)


def test_rest_retention_rejection_defers_to_vision_without_aborting(tmp_path) -> None:
    gap_store = GapParquetStore(tmp_path / "gaps")
    gap_store.upsert(
        (
            GapRecord(
                symbol="BTCUSDT",
                exchange="binanceusdm",
                kind=GapKind.STORAGE_HOLE,
                start=START,
                end=START + timedelta(hours=1),
                detected_at=START + timedelta(days=3),
            ),
        )
    )

    class RetentionLimited:
        def fetch(self, *_args, **_kwargs):
            request = httpx.Request("GET", "https://fapi.binance.com/fapi/v1/aggTrades")
            response = httpx.Response(
                400,
                request=request,
                json={"code": -4166, "msg": "recent 2 days only"},
            )
            raise httpx.HTTPStatusError(
                "outside retention",
                request=request,
                response=response,
            )

    report = recover_storage_gaps(
        data_root=tmp_path,
        candle_root=tmp_path / "candles",
        gap_root=tmp_path / "gaps",
        exchange="binanceusdm",
        symbols=["BTCUSDT"],
        fetcher=RetentionLimited(),
    )

    assert not report.recovered
    assert report.skipped_symbols == (f"BTCUSDT:{START.isoformat()}:vision",)


def test_closed_tail_is_materialized_and_recovered_after_restart(tmp_path) -> None:
    candle_store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    candle_store.upsert(
        (
            Candle(
                symbol="BTCUSDT",
                timeframe="1h",
                open_time=START,
                close_time=START + timedelta(hours=1),
                open=Decimal(100),
                high=Decimal(101),
                low=Decimal(99),
                close=Decimal(100),
                volume=Decimal(1),
                quote_volume=Decimal(100),
                trade_count=1,
            ),
        )
    )

    class TailFetcher:
        def fetch(self, symbol, start, end):
            assert symbol == "BTCUSDT"
            assert start == START + timedelta(hours=1)
            assert end == START + timedelta(hours=2)
            start_ms = int(start.timestamp() * 1_000)
            frame = pd.DataFrame(
                [
                    {
                        "ts_ms": start_ms + minute * 60_000,
                        "price": 101.0 + minute / 100,
                        "amount": 1.0,
                        "side": "buy" if minute % 2 == 0 else "sell",
                    }
                    for minute in range(60)
                ]
            )
            return FetchedTape(
                symbol="BTCUSDT",
                start=start,
                end=end,
                frame=frame,
                first_agg_id=20,
                last_agg_id=79,
                requests=1,
                sha256="tail-proof",
            )

    report = recover_storage_gaps(
        data_root=tmp_path,
        candle_root=tmp_path / "candles",
        gap_root=tmp_path / "gaps",
        exchange="binanceusdm",
        symbols=["BTCUSDT"],
        fetcher=TailFetcher(),
        recover_closed_tail=True,
        now=START + timedelta(hours=2, minutes=5),
    )

    assert len(report.recovered) == 1
    repaired = candle_store.read("BTCUSDT", "1h")
    assert any(candle.open_time == START + timedelta(hours=1) for candle in repaired)
    gaps = GapParquetStore(tmp_path / "gaps").read("binanceusdm", "BTCUSDT")
    assert len(gaps) == 1
    assert gaps[0].recovered is True
    assert "closed canonical tail missing" in gaps[0].detail


def test_scanner_tail_can_recover_at_closed_five_minute_boundary(tmp_path) -> None:
    candle_store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    candle_store.upsert(
        (
            Candle(
                symbol="BTCUSDT",
                timeframe="5m",
                open_time=START,
                close_time=START + timedelta(minutes=5),
                open=Decimal(100),
                high=Decimal(101),
                low=Decimal(99),
                close=Decimal(100),
                volume=Decimal(1),
                quote_volume=Decimal(100),
                trade_count=1,
            ),
        )
    )

    class FiveMinuteTailFetcher:
        def fetch(self, symbol, start, end):
            assert symbol == "BTCUSDT"
            assert start == START + timedelta(minutes=5)
            assert end == START + timedelta(minutes=10)
            start_ms = int(start.timestamp() * 1_000)
            frame = pd.DataFrame(
                [
                    {
                        "ts_ms": start_ms + minute * 60_000,
                        "price": 101.0 + minute / 100,
                        "amount": 1.0,
                        "side": "buy",
                    }
                    for minute in range(5)
                ]
            )
            return FetchedTape(
                symbol="BTCUSDT",
                start=start,
                end=end,
                frame=frame,
                first_agg_id=100,
                last_agg_id=104,
                requests=1,
                sha256="five-minute-tail",
            )

    report = recover_storage_gaps(
        data_root=tmp_path,
        candle_root=tmp_path / "candles",
        gap_root=tmp_path / "gaps",
        exchange="binanceusdm",
        symbols=["BTCUSDT"],
        fetcher=FiveMinuteTailFetcher(),
        recover_closed_tail=True,
        tail_timeframe="5m",
        now=START + timedelta(minutes=12),
    )

    assert len(report.recovered) == 1
    repaired = candle_store.read("BTCUSDT", "5m")
    assert [candle.open_time for candle in repaired] == [
        START,
        START + timedelta(minutes=5),
    ]
    gap = GapParquetStore(tmp_path / "gaps").read("binanceusdm", "BTCUSDT")[0]
    assert gap.recovered is True
    assert "coverage_timeframe=5m" in gap.detail


def test_scanner_recovery_materializes_an_interior_five_minute_hole(tmp_path) -> None:
    candle_store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")

    def candle(open_time: datetime) -> Candle:
        return Candle(
            symbol="BTCUSDT",
            timeframe="5m",
            open_time=open_time,
            close_time=open_time + timedelta(minutes=5),
            open=Decimal(100),
            high=Decimal(101),
            low=Decimal(99),
            close=Decimal(100),
            volume=Decimal(1),
            quote_volume=Decimal(100),
            trade_count=1,
        )

    candle_store.upsert((candle(START), candle(START + timedelta(minutes=10))))

    class InteriorFetcher:
        def fetch(self, symbol, start, end):
            assert symbol == "BTCUSDT"
            assert start == START + timedelta(minutes=5)
            assert end == START + timedelta(minutes=10)
            start_ms = int(start.timestamp() * 1_000)
            frame = pd.DataFrame(
                [
                    {
                        "ts_ms": start_ms + minute * 60_000,
                        "price": 100.0 + minute / 100,
                        "amount": 1.0,
                        "side": "buy",
                    }
                    for minute in range(5)
                ]
            )
            return FetchedTape(
                symbol="BTCUSDT",
                start=start,
                end=end,
                frame=frame,
                first_agg_id=200,
                last_agg_id=204,
                requests=1,
                sha256="interior-five-minute",
            )

    report = recover_storage_gaps(
        data_root=tmp_path,
        candle_root=tmp_path / "candles",
        gap_root=tmp_path / "gaps",
        exchange="binanceusdm",
        symbols=["BTCUSDT"],
        fetcher=InteriorFetcher(),
        recover_closed_tail=True,
        tail_timeframe="5m",
        now=START + timedelta(minutes=17),
    )

    assert len(report.recovered) == 1
    repaired = candle_store.read("BTCUSDT", "5m")
    assert [item.open_time for item in repaired] == [
        START,
        START + timedelta(minutes=5),
        START + timedelta(minutes=10),
    ]
    gap = GapParquetStore(tmp_path / "gaps").read("binanceusdm", "BTCUSDT")[0]
    assert gap.recovered is True
    assert "interior canonical hole" in gap.detail


def test_long_gap_recovery_commits_each_hour_and_resumes_delta(tmp_path) -> None:
    gap_store = GapParquetStore(tmp_path / "gaps")
    gap_store.upsert(
        (
            GapRecord(
                symbol="BTCUSDT",
                exchange="binanceusdm",
                kind=GapKind.STORAGE_HOLE,
                start=START,
                end=START + timedelta(hours=2),
                detected_at=START + timedelta(hours=3),
                detail="coverage_timeframe=1h",
            ),
        )
    )

    def tape(start, end, first_id):
        start_ms = int(start.timestamp() * 1_000)
        frame = pd.DataFrame(
            [
                {
                    "ts_ms": start_ms + minute * 60_000,
                    "price": 100.0 + minute / 100,
                    "amount": 1.0,
                    "side": "buy",
                }
                for minute in range(60)
            ]
        )
        return FetchedTape(
            symbol="BTCUSDT",
            start=start,
            end=end,
            frame=frame,
            first_agg_id=first_id,
            last_agg_id=first_id + 59,
            requests=1,
            sha256=f"proof-{first_id}",
        )

    class InterruptedAfterFirstHour:
        calls = 0

        def fetch(self, symbol, start, end):
            assert symbol == "BTCUSDT"
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("simulated restart")
            return tape(start, end, 100)

    with pytest.raises(RuntimeError, match="simulated restart"):
        recover_storage_gaps(
            data_root=tmp_path,
            candle_root=tmp_path / "candles",
            gap_root=tmp_path / "gaps",
            exchange="binanceusdm",
            symbols=["BTCUSDT"],
            fetcher=InterruptedAfterFirstHour(),
        )

    stored = CandleParquetStore(
        tmp_path / "candles", exchange="binanceusdm"
    ).read("BTCUSDT", "1h")
    assert [candle.open_time for candle in stored] == [START]

    class ResumeSecondHourOnly:
        def fetch(self, symbol, start, end):
            assert symbol == "BTCUSDT"
            assert start == START + timedelta(hours=1)
            assert end == START + timedelta(hours=2)
            return tape(start, end, 200)

    report = recover_storage_gaps(
        data_root=tmp_path,
        candle_root=tmp_path / "candles",
        gap_root=tmp_path / "gaps",
        exchange="binanceusdm",
        symbols=["BTCUSDT"],
        fetcher=ResumeSecondHourOnly(),
    )

    assert len(report.recovered) == 1
    assert report.recovered[0].start == (START + timedelta(hours=1)).isoformat()
    assert gap_store.read("binanceusdm", "BTCUSDT")[0].recovered is True
