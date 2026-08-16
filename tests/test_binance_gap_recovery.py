from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pandas as pd
import pytest

from vnedge.data.binance_gap_recovery import BinanceAggTradeRest, _write_tape

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
