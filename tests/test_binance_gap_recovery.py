from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pandas as pd
import pytest

from vnedge.data.binance_gap_recovery import (
    BinanceAggTradeRest,
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
    candle_store.upsert((
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
    ))
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
    gap_store.upsert((
        GapRecord(
            symbol="BTCUSDT",
            exchange="binanceusdm",
            kind=GapKind.STORAGE_HOLE,
            start=START,
            end=START + timedelta(hours=1),
            detected_at=START + timedelta(days=3),
        ),
    ))

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
    assert report.skipped_symbols == (
        f"BTCUSDT:{START.isoformat()}:vision",
    )
