"""Incremental lane startup: strategy-sized history, cache range fill, and an
immediate placeholder.  An overlapping cache fetches only its missing prefix,
tail, or internal holes; an absent/corrupt/non-overlapping cache fetches the
requested window.  Correctness never depends on synthetic candles."""

from __future__ import annotations

import asyncio

from vnedge.runtime.multi_lane import (
    LaneSpec,
    MultiLaneProvider,
    _load_candle_cache,
    _required_warmup_bars,
    _timeframe_ms,
    _warmup_bars,
    _warmup_candles,
)
from vnedge.runtime.runner_config import RunnerMode

_TF = 300_000  # 5m in ms


class _FakeRest:
    """Records fetch ranges and emits candles across each requested window."""

    def __init__(self):
        self.calls: list[tuple[int, int]] = []

    async def fetch_candles(self, symbol, timeframe, since, until):
        self.calls.append((since, until))
        return [[t, 100.0, 101.0, 99.0, 100.5, 10.0] for t in range(since, until, _TF)]


def _spec() -> LaneSpec:
    return LaneSpec(lane_id="lane-a", exchange="binanceusdm",
                    symbol="BTC/USDT:USDT", timeframe="5m", mode=RunnerMode.SHADOW)


def test_bars_based_lookback_is_per_timeframe():
    assert _timeframe_ms("5m") == 300_000
    assert _timeframe_ms("4h") == 14_400_000
    assert _timeframe_ms("nonsense") == 3_600_000  # safe 1h default
    assert _warmup_bars({"MULTI_LANE_WARMUP_BARS": "600"}) == 600
    assert _warmup_bars({}) == 500
    assert _warmup_bars({"MULTI_LANE_WARMUP_BARS": "x"}) == 500  # bad value -> default


def test_strategy_warmup_expands_only_lanes_that_need_more_history():
    squeeze = LaneSpec(
        lane_id="squeeze",
        exchange="binanceusdm",
        symbol="BTC/USDT:USDT",
        timeframe="5m",
        strategy_id="squeeze_expansion_breakout_v3",
    )
    measurement = _spec()
    assert _required_warmup_bars(squeeze, {}) == 2066
    assert _required_warmup_bars(measurement, {}) == 500


def test_current_scanner_ids_use_their_frozen_warmup_contracts():
    range_v4 = LaneSpec(
        lane_id="range-v4",
        exchange="binanceusdm",
        symbol="BTC/USDT:USDT",
        timeframe="15m",
        strategy_id="range_expansion_observer_v4",
    )
    bos_v3 = LaneSpec(
        lane_id="bos-v3",
        exchange="binanceusdm",
        symbol="BTC/USDT:USDT",
        timeframe="15m",
        strategy_id="structure_bos_15m_trigger_v3",
    )

    assert _required_warmup_bars(range_v4, {}) == 2018
    # The global operational floor exceeds BoS v3's 224-bar feature need.
    assert _required_warmup_bars(bos_v3, {}) == 500


def test_first_run_full_fetches_and_writes_cache(tmp_path):
    cache = tmp_path / "lane-a.candles.parquet"
    rest = _FakeRest()
    frame = asyncio.run(_warmup_candles(rest, _spec(), cache, 0, 500 * _TF))
    assert len(frame) >= 499
    assert cache.exists()
    assert len(rest.calls) == 1  # one full fetch


def test_second_run_fetches_only_the_gap(tmp_path):
    cache = tmp_path / "lane-a.candles.parquet"
    asyncio.run(_warmup_candles(_FakeRest(), _spec(), cache, 0, 500 * _TF))  # seed cache
    # window shifts forward 10 bars -> only the delta should be fetched
    rest = _FakeRest()
    frame = asyncio.run(_warmup_candles(rest, _spec(), cache, 10 * _TF, 510 * _TF))
    gap_since = rest.calls[0][0]
    assert gap_since > 10 * _TF  # started AFTER the cached window, not at `since`
    assert len(frame) >= 499


def test_larger_strategy_window_fetches_only_missing_prefix(tmp_path):
    cache = tmp_path / "lane-a.candles.parquet"
    # The old lane retained the newest 500 bars of a 2,000-bar requested range.
    asyncio.run(
        _warmup_candles(
            _FakeRest(), _spec(), cache, 1500 * _TF, 2000 * _TF
        )
    )

    rest = _FakeRest()
    frame = asyncio.run(_warmup_candles(rest, _spec(), cache, 0, 2000 * _TF))

    assert rest.calls == [(0, 1500 * _TF)]
    assert len(frame) == 2000


def test_cache_internal_hole_fetches_only_that_missing_range(tmp_path):
    cache = tmp_path / "lane-a.candles.parquet"
    asyncio.run(_warmup_candles(_FakeRest(), _spec(), cache, 0, 10 * _TF))
    cached = _load_candle_cache(cache)
    assert cached is not None
    cached = cached.drop(index=5).reset_index(drop=True)
    cached.to_parquet(cache, index=False)

    rest = _FakeRest()
    frame = asyncio.run(_warmup_candles(rest, _spec(), cache, 0, 10 * _TF))

    assert rest.calls == [(5 * _TF, 6 * _TF)]
    assert len(frame) == 10


def test_corrupt_cache_falls_back_to_full_fetch(tmp_path):
    cache = tmp_path / "lane-a.candles.parquet"
    cache.write_text("not a parquet file")
    assert _load_candle_cache(cache) is None
    rest = _FakeRest()
    frame = asyncio.run(_warmup_candles(rest, _spec(), cache, 0, 500 * _TF))
    assert len(frame) >= 499  # recovered via full fetch
    assert len(rest.calls) == 1


def test_warmup_drops_current_forming_exchange_candle(tmp_path):
    class _InclusiveRest(_FakeRest):
        async def fetch_candles(self, symbol, timeframe, since, until):
            rows = await super().fetch_candles(symbol, timeframe, since, until)
            # Common REST behavior: include the bucket that opened at `until`
            # even though it cannot be closed yet.
            rows.append([until, 100.0, 101.0, 99.0, 100.5, 1.0])
            return rows

    frame = asyncio.run(
        _warmup_candles(
            _InclusiveRest(),
            _spec(),
            tmp_path / "lane-a.candles.parquet",
            0,
            10 * _TF,
        )
    )

    assert len(frame) == 10
    assert int(frame["timestamp"].iloc[-1].timestamp() * 1000) == 9 * _TF


def test_warming_placeholder_shows_the_fleet_immediately():
    provider = MultiLaneProvider("lane-a")
    provider.publish_warming("lane-a", "binanceusdm", "BTC/USDT:USDT")
    snap = provider.latest()
    assert snap is not None
    lane = snap["lanes"][0]
    assert lane["risk_status"] == "warming"
    assert snap["mode"] == "warming up"


# --- warmup-burst resilience: bounded concurrency + transient retry --------------

import pytest
from ccxt.base.errors import ExchangeError, NetworkError

from vnedge.runtime.multi_lane import _lane_build_concurrency, _retry_transient


def test_lane_build_concurrency_reads_env_with_safe_default():
    assert _lane_build_concurrency({}) == 6
    assert _lane_build_concurrency({"MULTI_LANE_BUILD_CONCURRENCY": "10"}) == 10
    assert _lane_build_concurrency({"MULTI_LANE_BUILD_CONCURRENCY": "x"}) == 6
    assert _lane_build_concurrency({"MULTI_LANE_BUILD_CONCURRENCY": "0"}) == 1  # floor


def test_retry_transient_returns_on_first_success():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return "ok"

    out = asyncio.run(_retry_transient(factory, retries=3, backoff_s=0, label="lane"))
    assert out == "ok" and calls["n"] == 1


def test_retry_transient_recovers_after_transient_network_errors():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise NetworkError("rate limited")
        return "recovered"

    out = asyncio.run(_retry_transient(factory, retries=3, backoff_s=0, label="lane"))
    assert out == "recovered" and calls["n"] == 3


def test_retry_transient_raises_after_exhausting_retries():
    async def factory():
        raise NetworkError("still down")

    with pytest.raises(NetworkError):
        asyncio.run(_retry_transient(factory, retries=3, backoff_s=0, label="lane"))


def test_retry_transient_does_not_retry_non_network_errors():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise ExchangeError("bad symbol")  # a real error, not transient

    with pytest.raises(ExchangeError):
        asyncio.run(_retry_transient(factory, retries=3, backoff_s=0, label="lane"))
    assert calls["n"] == 1  # raised immediately, no retry
