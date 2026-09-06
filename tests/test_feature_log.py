"""Feature log: off-loop worker, identity, full fingerprint, fail-soft."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from vnedge.ml.feature_log import (
    FEATURE_LOG_SCHEMA_VERSION,
    FeatureLogWriter,
    feature_fingerprint,
    read_feature_log,
)
from vnedge.ml.feature_matrix import FEATURE_COLUMNS, FeatureParams


def _frame(n: int = 420, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 30_000 + np.cumsum(rng.normal(0.0, 30.0, n))
    spread = np.abs(rng.normal(20.0, 12.0, n)) + 5.0
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
            "open": close + rng.normal(0.0, 10.0, n),
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": np.abs(rng.normal(100.0, 30.0, n)) + 1.0,
        }
    )


def _writer(tmp_path: Path, **kw) -> FeatureLogWriter:
    return FeatureLogWriter(
        tmp_path / "lane.features.jsonl",
        strategy_id="s1", symbol="BTC/USDT:USDT", timeframe="1h",
        exchange="binanceusdm", lane_id="L1", **kw,
    )


def test_row_carries_full_identity_and_contract(tmp_path: Path):
    writer = _writer(tmp_path)
    frame = _frame()
    assert writer.enqueue(
        frame, len(frame) - 1, decision="fired", bar_ts="2026-01-18T11:00:00+00:00",
        decision_id="D-42", side="long",
    )
    writer.close()
    record = json.loads(writer.path.read_text().splitlines()[0])
    assert record["v"] == FEATURE_LOG_SCHEMA_VERSION
    assert record["fingerprint"] == writer.fingerprint
    assert record["decision_id"] == "D-42" and record["side"] == "long"
    assert record["exchange"] == "binanceusdm" and record["lane"] == "L1"
    assert record["decision"] == "fired" and record["backfill"] is False
    assert len(record["decision_bar_hash"]) == 16
    assert set(record["features"].keys()) == set(FEATURE_COLUMNS)
    assert writer.rows_written == 1 and writer.errors == 0


def test_worker_runs_off_the_calling_thread(tmp_path: Path):
    writer = _writer(tmp_path)
    frame = _frame()
    writer.enqueue(frame, len(frame) - 1, decision="pass", bar_ts="a")
    assert writer.rows_written == 0  # not computed inline on the caller
    writer.flush()
    assert writer.rows_written == 1
    writer.close()


def test_snapshot_is_immutable_against_later_mutation(tmp_path: Path):
    writer = _writer(tmp_path)
    frame = _frame()
    idx = len(frame) - 1
    writer.enqueue(frame, idx, decision="pass", bar_ts="a")
    frame.iloc[idx, frame.columns.get_loc("close")] *= 2.0  # mutate after enqueue
    writer.flush()
    writer.close()
    from vnedge.ml.feature_matrix import build_feature_matrix

    original = _frame()
    expected = build_feature_matrix(original, None, FeatureParams()).iloc[idx]
    logged = json.loads(writer.path.read_text().splitlines()[0])["features"]
    assert logged["ret_1"] is not None
    assert abs(logged["ret_1"] - float(expected["ret_1"])) < 1e-9


def test_enqueue_is_fail_soft_on_garbage(tmp_path: Path):
    writer = _writer(tmp_path)
    bad = pd.DataFrame({"nope": [1, 2, 3]})
    writer.enqueue(bad, 1, decision="pass", bar_ts="x")
    writer.flush()
    writer.close()
    assert writer.rows_written == 0  # nothing raised, nothing written


def test_fingerprint_covers_params_and_inputs():
    base = feature_fingerprint(FeatureParams())
    changed = feature_fingerprint(FeatureParams(vol_window=99))
    assert base != changed, "params change must change the fingerprint"
    with_funding = feature_fingerprint(FeatureParams(), ("funding",))
    assert with_funding != base, "optional-input contract must change it"


def test_read_refuses_mixed_and_unexpected_fingerprints(tmp_path: Path):
    writer = _writer(tmp_path)
    frame = _frame()
    writer.enqueue(frame, len(frame) - 1, decision="pass", bar_ts="b1")
    writer.enqueue(frame, len(frame) - 2, decision="fired", bar_ts="b0")
    writer.close()
    loaded = read_feature_log([writer.path], expected_fingerprint=writer.fingerprint)
    assert len(loaded) == 2 and set(FEATURE_COLUMNS) <= set(loaded.columns)
    assert sorted(loaded["decision"]) == ["fired", "pass"]

    lines = writer.path.read_text().splitlines()
    bad = json.loads(lines[0])
    bad["fingerprint"] = "deadbeefdeadbeef"
    writer.path.write_text("\n".join([json.dumps(bad), lines[1]]) + "\n")
    for kwargs in ({}, {"expected_fingerprint": writer.fingerprint}):
        try:
            read_feature_log([writer.path], **kwargs)
            raise AssertionError("mismatched fingerprint must raise")
        except ValueError:
            pass
