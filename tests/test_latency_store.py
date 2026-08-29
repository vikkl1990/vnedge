"""Latency checkpoints resume p95 evidence instead of restarting warmup."""

from __future__ import annotations

import json

from vnedge.runtime.latency_store import LaneLatencyStore, RecorderLatencyStore
from vnedge.runtime.latency_tracker import LatencyTracker


def _tracker(count: int = 12) -> LatencyTracker:
    tracker = LatencyTracker(maxlen=20)
    for value in range(count):
        tracker.record("bar_close_processing_ms", float(value + 1))
        tracker.record("decision_lag_ms", float(value) / 10.0)
    return tracker


def test_latency_store_restores_then_collects_only_the_delta(tmp_path):
    path = tmp_path / "lane-a.latency.json"
    store = LaneLatencyStore(path, "lane-a")
    store.save_from(_tracker(12))

    restarted = LatencyTracker(maxlen=20)
    assert store.restore_into(restarted) is True
    assert restarted.stats("bar_close_processing_ms")["n"] == 12
    for value in range(8):
        restarted.record("bar_close_processing_ms", 20.0 + value)
        restarted.record("decision_lag_ms", 2.0 + value)
    assert restarted.stats("bar_close_processing_ms")["n"] == 20
    assert restarted.stats("decision_lag_ms")["n"] == 20


def test_latency_store_is_atomic_and_lane_scoped(tmp_path):
    path = tmp_path / "lane.latency.json"
    LaneLatencyStore(path, "lane-a").save_from(_tracker())
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 1
    assert payload["lane_id"] == "lane-a"
    assert not (tmp_path / "lane.latency.json.tmp").exists()

    other = LatencyTracker()
    assert LaneLatencyStore(path, "lane-b").restore_into(other) is False
    assert other.snapshot() == {}


def test_latency_store_missing_corrupt_or_nonfinite_is_ignored(tmp_path):
    missing = LatencyTracker()
    assert LaneLatencyStore(tmp_path / "missing.json", "x").restore_into(missing) is False

    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("{not json")
    assert LaneLatencyStore(corrupt_path, "x").restore_into(LatencyTracker()) is False

    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "lane_id": "x",
                "tracker": {
                    "version": 1,
                    "series": {"decision_lag_ms": [1.0, float("nan")]},
                },
            }
        )
    )
    restored = LatencyTracker()
    assert LaneLatencyStore(invalid_path, "x").restore_into(restored) is False
    assert restored.snapshot() == {}


def test_recorder_latency_store_is_a_separate_atomic_snapshot(tmp_path):
    path = tmp_path / "recorder" / "binanceusdm.json"
    tracker = LatencyTracker()
    tracker.record("trade_ingest_ms", 12.5)
    tracker.record("parquet_persist_ms", 4.0)

    store = RecorderLatencyStore(path, process_id="pulse-recorder:binanceusdm")
    store.save_from(tracker)

    payload = store.load()
    assert payload["process"] == "pulse-recorder:binanceusdm"
    assert payload["latency"]["trade_ingest_ms"]["p95"] == 12.5
    assert payload["latency"]["parquet_persist_ms"]["p95"] == 4.0
    assert not path.with_suffix(".json.tmp").exists()
