"""Feature log: rows, fingerprint contract, fail-soft, round-trip."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from vnedge.ml.feature_log import (
    FEATURE_LOG_SCHEMA_VERSION,
    FeatureLogWriter,
    feature_columns_fingerprint,
    read_feature_log,
)
from vnedge.ml.feature_matrix import FEATURE_COLUMNS


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


def _writer(tmp_path: Path) -> FeatureLogWriter:
    return FeatureLogWriter(
        tmp_path / "lane.features.jsonl",
        strategy_id="test_strategy",
        symbol="BTC/USDT:USDT",
        timeframe="1h",
    )


def test_append_writes_full_contract_row(tmp_path: Path):
    writer = _writer(tmp_path)
    frame = _frame()
    ok = writer.append(frame, len(frame) - 1, decision="fired",
                       bar_ts="2026-01-18T11:00:00+00:00", intent_key="k1")
    assert ok and writer.rows_written == 1 and writer.errors == 0
    record = json.loads(writer.path.read_text().splitlines()[0])
    assert record["v"] == FEATURE_LOG_SCHEMA_VERSION
    assert record["fingerprint"] == feature_columns_fingerprint()
    assert record["decision"] == "fired" and record["intent_key"] == "k1"
    assert set(record["features"].keys()) == set(FEATURE_COLUMNS)
    # numeric features are numbers-or-null, never NaN strings
    assert all(v is None or isinstance(v, (int, float)) for v in record["features"].values())


def test_append_is_fail_soft_on_garbage(tmp_path: Path):
    writer = _writer(tmp_path)
    bad = pd.DataFrame({"nope": [1, 2, 3]})
    ok = writer.append(bad, 1, decision="pass", bar_ts="x")
    assert ok is False and writer.errors == 1
    assert not writer.path.exists()


def test_same_bar_appends_reuse_one_matrix(tmp_path: Path):
    writer = _writer(tmp_path)
    frame = _frame()
    calls = {"n": 0}
    original = writer._matrix_for

    def counting(f):
        calls["n"] += 1
        return original(f)

    # count underlying builds via the cache key: two appends, same bar
    writer.append(frame, len(frame) - 1, decision="pass", bar_ts="a")
    cached = writer._cache_frame is not None
    writer.append(frame, len(frame) - 1, decision="fired", bar_ts="a")
    assert cached and writer.rows_written == 2


def test_read_round_trip_and_mixed_fingerprint_refusal(tmp_path: Path):
    writer = _writer(tmp_path)
    frame = _frame()
    writer.append(frame, len(frame) - 1, decision="pass", bar_ts="b1")
    writer.append(frame, len(frame) - 2, decision="fired", bar_ts="b0")
    loaded = read_feature_log([writer.path])
    assert len(loaded) == 2
    assert set(FEATURE_COLUMNS) <= set(loaded.columns)
    assert sorted(loaded["decision"]) == ["fired", "pass"]

    # corrupt one row's fingerprint -> reader must refuse the blend
    lines = writer.path.read_text().splitlines()
    bad = json.loads(lines[0])
    bad["fingerprint"] = "deadbeefdeadbeef"
    writer.path.write_text("\n".join([json.dumps(bad), lines[1]]) + "\n")
    try:
        read_feature_log([writer.path])
        raise AssertionError("mixed fingerprints must raise")
    except ValueError:
        pass
