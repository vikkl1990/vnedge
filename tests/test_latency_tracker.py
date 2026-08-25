import math
from datetime import UTC, datetime, timedelta

import pytest

from vnedge.runtime.latency_thresholds import decision_compute_limits
from vnedge.runtime.latency_tracker import (
    BAR_CLOSE_PROCESSING_MS,
    BAR_CLOSE_RECEIPT_MS,
    CANONICAL_WAIT_MS,
    CLOCK_SKEW_MS,
    INGEST_LAG_MS,
    LatencyTracker,
    timeframe_to_seconds,
)


def test_decision_compute_limits_are_strict_by_default_and_scale_explicitly():
    assert decision_compute_limits("1m") == (50, 200, 100)
    assert decision_compute_limits("unknown") == (50, 200, 100)
    assert decision_compute_limits("5m") == (100, 500, 350)
    assert decision_compute_limits("15M") == (500, 2500, 2000)


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
    assert s == {
        "last": 7.5,
        "p50": 7.5,
        "p95": 7.5,
        "max": 7.5,
        "n": 1,
        "recent": [7.5],
    }


def test_stats_exposes_only_five_newest_samples_in_order():
    tracker = LatencyTracker()
    for value in range(8):
        tracker.record("x", float(value))

    assert tracker.stats("x")["recent"] == [3.0, 4.0, 5.0, 6.0, 7.0]


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


def test_raw_state_resumes_only_missing_samples_and_keeps_order():
    before = LatencyTracker(maxlen=20)
    for value in range(12):
        before.record("decision_lag_ms", float(value))

    restarted = LatencyTracker(maxlen=20)
    assert restarted.restore_state(before.export_state()) == 12
    for value in range(12, 20):
        restarted.record("decision_lag_ms", float(value))

    stats = restarted.stats("decision_lag_ms")
    assert stats["n"] == 20
    assert stats["last"] == 19.0
    assert stats["recent"] == [15.0, 16.0, 17.0, 18.0, 19.0]


def test_restore_uses_current_bound_and_rejects_partial_corruption():
    restarted = LatencyTracker(maxlen=3)
    assert restarted.restore_state(
        {"version": 1, "maxlen": 999, "series": {"x": [1, 2, 3, 4]}}
    ) == 3
    assert restarted.stats("x")["n"] == 3
    assert restarted.stats("x")["last"] == 4.0

    with pytest.raises(ValueError, match="non-finite"):
        restarted.restore_state(
            {"version": 1, "series": {"x": [1.0, float("nan")]}}
        )
    # Rejection is atomic: the prior valid tracker was not partially replaced.
    assert restarted.stats("x")["n"] == 3


def test_restore_rebaselines_pre_receipt_close_history() -> None:
    tracker = LatencyTracker()
    restored = tracker.restore_state(
        {
            "version": 1,
            "series": {
                "bar_close_processing_ms": [8_000.0],
                "feed_lag_ms": [8_000.0],
                "decision_lag_ms": [12.0],
            },
        }
    )

    assert restored == 1
    assert tracker.stats(BAR_CLOSE_PROCESSING_MS) is None
    assert tracker.stats("feed_lag_ms") is None
    assert tracker.stats("decision_lag_ms")["last"] == 12.0


def test_event_latency_separates_ingest_from_future_clock_skew():
    tracker = LatencyTracker()
    base = datetime(2026, 8, 16, 12, tzinfo=UTC)

    normal = tracker.record_event(base, base + timedelta(milliseconds=75))
    future = tracker.record_event(base + timedelta(milliseconds=40), base)

    assert normal == {INGEST_LAG_MS: 75.0, CLOCK_SKEW_MS: 0.0}
    assert future == {INGEST_LAG_MS: 0.0, CLOCK_SKEW_MS: 40.0}
    assert tracker.stats(INGEST_LAG_MS)["n"] == 2
    assert tracker.stats(CLOCK_SKEW_MS)["max"] == 40.0


def test_closed_bar_metric_keeps_legacy_alias_exactly_equal():
    tracker = LatencyTracker()
    close = datetime(2026, 8, 16, 12, tzinfo=UTC)

    result = tracker.record_bar_close(close, close + timedelta(milliseconds=320))

    assert result[BAR_CLOSE_PROCESSING_MS] == 320.0
    assert result[BAR_CLOSE_RECEIPT_MS] == 320.0
    assert tracker.stats(BAR_CLOSE_RECEIPT_MS) == tracker.stats("feed_lag_ms")
    assert tracker.stats(BAR_CLOSE_PROCESSING_MS) == tracker.stats("feed_lag_ms")

    tracker.record_canonical_wait(48.0)
    assert tracker.stats(CANONICAL_WAIT_MS)["last"] == 48.0


def test_latency_boundaries_reject_naive_datetimes():
    tracker = LatencyTracker()
    aware = datetime(2026, 8, 16, 12, tzinfo=UTC)
    naive = aware.replace(tzinfo=None)

    with pytest.raises(ValueError, match="timezone-aware"):
        tracker.record_event(naive, aware)
    with pytest.raises(ValueError, match="timezone-aware"):
        tracker.record_bar_close(aware, naive)
