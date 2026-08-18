"""Tests for the causal VWAP drift measurement module."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vnedge.research.vwap_drift import (
    VwapDriftConfig,
    annotate_vwap_drift,
    drift_bucket_table,
    expansion_day_drift_profile,
    forward_return_bps,
    rolling_vwap,
    s4_filter_diagnostic,
    side_of_vwap_hit_rate,
    signed_drift_bps,
)


def test_vwap_excludes_the_current_bar() -> None:
    close = pd.Series([10.0, 10.0, 12.0, 12.0])
    volume = pd.Series([1.0, 1.0, 100.0, 1.0])
    vwap = rolling_vwap(close, volume, window=2, min_periods=1)
    # at i=2 only bars 0..1 (both 10) may contribute -- the heavy 12 is current
    assert vwap.iloc[2] == pytest.approx(10.0)


def test_signed_drift_sign_and_scale() -> None:
    assert signed_drift_bps(pd.Series([102.0]), pd.Series([100.0])).iloc[0] == pytest.approx(
        200.0
    )


def test_forward_return_is_a_label_not_a_feature() -> None:
    close = pd.Series([100.0, 101.0, 102.0])
    fwd = forward_return_bps(close, 1)
    assert fwd.iloc[0] == pytest.approx(100.0)
    assert np.isnan(fwd.iloc[-1])  # last bar has no future


def test_annotate_emits_columns_and_preserves_unknown_side() -> None:
    n = 300
    frame = pd.DataFrame(
        {
            "close": np.linspace(100, 101, n),
            "volume": np.ones(n),
            "high": np.linspace(100, 101, n) + 0.1,
            "low": np.linspace(100, 101, n) - 0.1,
        }
    )
    out = annotate_vwap_drift(frame)
    for column in ("vwap", "vwap_drift_bps", "vwap_abs_drift_bps", "above_vwap"):
        assert column in out.columns
    assert out["vwap"].notna().any()
    # warmup rows have no VWAP: the side flag must stay unknown, not "below"
    assert out["above_vwap"].isna().sum() > 0


def test_bucket_table_covers_all_rows() -> None:
    rng = np.random.default_rng(5)
    n = 800
    close = 100 + np.cumsum(rng.normal(0, 0.05, n))
    frame = pd.DataFrame({"close": close, "volume": np.full(n, 10.0)})
    table = drift_bucket_table(frame, config=VwapDriftConfig(vwap_bars=48, min_periods=24))
    assert len(table) == len(VwapDriftConfig().bucket_edges_bps) + 1
    graded = annotate_vwap_drift(frame, VwapDriftConfig(vwap_bars=48, min_periods=24))
    assert table["n"].sum() == int(graded["vwap_drift_bps"].notna().sum())


def test_side_hit_rate_is_a_probability() -> None:
    rng = np.random.default_rng(9)
    n = 600
    close = 100 + np.cumsum(rng.normal(0, 0.05, n))
    frame = pd.DataFrame({"close": close, "volume": np.full(n, 10.0)})
    out = annotate_vwap_drift(frame, VwapDriftConfig(vwap_bars=48, min_periods=24))
    stats = side_of_vwap_hit_rate(out, forward_bars=6)
    assert stats["n"] > 0
    assert 0.0 <= stats["hit_rate"] <= 1.0


def test_expansion_day_profile_flags_a_wide_day() -> None:
    index = pd.date_range("2026-08-17", periods=288, freq="5min", tz="UTC")
    close = np.linspace(60_000, 61_800, 288)  # ~300 bps trend day
    frame = pd.DataFrame(
        {"close": close, "volume": np.full(288, 10.0), "high": close + 5, "low": close - 5},
        index=index,
    )
    out = annotate_vwap_drift(frame, VwapDriftConfig(vwap_bars=48, min_periods=12))
    profile = expansion_day_drift_profile(out, range_bps_threshold=200.0)
    assert len(profile) == 1
    assert bool(profile.iloc[0]["expansion"])
    assert profile.iloc[0]["drift_close"] > 0  # trend day closes stretched above VWAP


def test_s4_diagnostic_reports_vetoed_counts() -> None:
    n = 400
    close = pd.Series(np.linspace(100, 104, n))
    frame = pd.DataFrame({"close": close, "volume": np.full(n, 10.0)})
    out = annotate_vwap_drift(frame, VwapDriftConfig(vwap_bars=48, min_periods=24))
    longs = pd.Series(False, index=out.index)
    shorts = pd.Series(False, index=out.index)
    longs.iloc[300] = True
    shorts.iloc[320] = True  # counter-trend short in a rising tape -> vetoed
    report = s4_filter_diagnostic(out, signal_long=longs, signal_short=shorts)
    assert report["long_raw"]["n"] == 1.0
    assert report["vetoed_shorts"] == 1
