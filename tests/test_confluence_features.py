"""Confluence features: causality, neutral optional inputs, contract."""

from __future__ import annotations

import numpy as np
import pandas as pd

from vnedge.ml.confluence_features import (
    CONFLUENCE_FEATURE_COLUMNS,
    ConfluenceParams,
    add_confluence_features,
)


def _frame(n: int = 400, seed: int = 5) -> pd.DataFrame:
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


def test_confluence_features_are_causal_under_future_mutation():
    base = _frame()
    rng = np.random.default_rng(9)
    oi = pd.Series(1e9 + np.cumsum(rng.normal(0, 1e6, len(base))), index=base.index)
    bench = pd.Series(
        60_000 + np.cumsum(rng.normal(0.0, 50.0, len(base))), index=base.index
    )
    mutated = base.copy()
    mutated.loc[320:, ["open", "high", "low", "close"]] *= 1.4
    mutated.loc[320:, "volume"] *= 8.0
    oi_mutated = oi.copy()
    oi_mutated.iloc[320:] *= 3.0
    bench_mutated = bench.copy()
    bench_mutated.iloc[320:] *= 0.5

    params = ConfluenceParams()
    a = add_confluence_features(base, params, open_interest=oi, benchmark_close=bench)
    b = add_confluence_features(
        mutated, params, open_interest=oi_mutated, benchmark_close=bench_mutated
    )
    for col in CONFLUENCE_FEATURE_COLUMNS:
        va, vb = a[col].iloc[300], b[col].iloc[300]
        assert (pd.isna(va) and pd.isna(vb)) or va == vb, (
            f"{col} at row 300 changed when only future rows were mutated"
        )


def test_optional_inputs_absent_yield_neutral_not_nan():
    params = ConfluenceParams()
    out = add_confluence_features(_frame(), params)
    tail = out.iloc[params.warmup_bars :]
    for col in ("oi_z", "oi_change", "oi_price_div", "rel_ret", "rel_strength_z"):
        assert (tail[col] == 0.0).all(), f"{col} not neutral without its input"
    for col in ("rsi14", "macd_hist_bps", "stoch_k", "obv_z", "is_weekend"):
        assert not tail[col].isna().any(), f"{col} went NaN past warmup"


def test_divergence_flags_are_binary_and_fire_somewhere():
    out = add_confluence_features(_frame(800, seed=2), ConfluenceParams())
    for col in ("bull_div_rsi", "bear_div_rsi", "bull_div_obv", "bear_div_obv"):
        assert set(out[col].dropna().unique()) <= {0.0, 1.0}
    fired = sum(
        out[col].sum()
        for col in ("bull_div_rsi", "bear_div_rsi")
    )
    assert fired > 0, "RSI divergence flags never fired on a random walk"


def test_feature_matrix_contract_includes_confluence_columns():
    from vnedge.ml.feature_matrix import FEATURE_COLUMNS, FeatureParams, build_feature_matrix

    for col in CONFLUENCE_FEATURE_COLUMNS:
        assert col in FEATURE_COLUMNS
    params = FeatureParams()
    out = build_feature_matrix(_frame(600), None, params)
    tail = out.iloc[params.warmup_bars :]
    assert len(tail) > 0
    assert not tail["rsi14"].isna().any()
    assert (tail["is_weekend"].isin([0.0, 1.0])).all()
