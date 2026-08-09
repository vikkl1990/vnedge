import math

import pytest

from vnedge.runtime.latency_tracker import (
    LatencyTracker,
    timeframe_to_seconds,
)


@pytest.mark.parametrize(
    "tf,secs",
    [("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600), ("4h", 14400), ("1d", 86400)],
)
def test_timeframe_to_seconds(tf, secs):
    assert timeframe_to_seconds(tf) == secs


@pytest.mark.parametrize("bad", ["", "m", "1x", "abc", "1", "h1", None])
def test_timeframe_to_seconds_rejects_garbage(bad):
    # a wrong bar length silently poisons every feed-lag number -> fail loud
    with pytest.raises(ValueError):
        timeframe_to_seconds(bad)


def test_empty_tracker_reports_nothing():
    t = LatencyTracker()
    assert t.stats("feed_lag_ms") is None
    assert t.snapshot() == {}


def test_stats_percentiles_and_last():
    t = LatencyTracker()
    for v in range(1, 101):  # 1..100
        t.record("feed_lag_ms", float(v))
    s = t.stats("feed_lag_ms")
    assert s["n"] == 100
    assert s["last"] == 100.0
    assert s["max"] == 100.0
    # nearest-rank on 1..100: p50 ~ 50, p95 ~ 95
    assert s["p50"] == pytest.approx(50, abs=1)
    assert s["p95"] == pytest.approx(95, abs=1)


def test_single_sample():
    t = LatencyTracker()
    t.record("decision_lag_ms", 7.5)
    s = t.stats("decision_lag_ms")
    assert s == {"last": 7.5, "p50": 7.5, "p95": 7.5, "max": 7.5, "n": 1}


def test_rolling_window_evicts_old():
    t = LatencyTracker(maxlen=3)
    for v in (10, 20, 30, 40):
        t.record("x", float(v))
    s = t.stats("x")
    assert s["n"] == 3  # 10 evicted
    assert s["last"] == 40.0
    assert s["max"] == 40.0


def test_non_finite_samples_dropped():
    # a NaN/inf would break /state JSON (allow_nan=False) — must never land
    t = LatencyTracker()
    t.record("x", float("nan"))
    t.record("x", float("inf"))
    t.record("x", float("-inf"))
    assert t.stats("x") is None
    t.record("x", 5.0)
    assert t.stats("x")["n"] == 1


def test_snapshot_multi_metric():
    t = LatencyTracker()
    t.record("feed_lag_ms", 1200.0)
    t.record("decision_lag_ms", 3.0)
    snap = t.snapshot()
    assert set(snap) == {"feed_lag_ms", "decision_lag_ms"}
    assert snap["feed_lag_ms"]["last"] == 1200.0
    # snapshot must be JSON-clean (finite floats only)
    for st in snap.values():
        for v in st.values():
            assert not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))
