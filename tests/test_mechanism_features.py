"""Mechanism features: causality, saturation, and matrix integration."""

from __future__ import annotations

import numpy as np
import pandas as pd

from vnedge.ml.mechanism_features import (
    MECHANISM_FEATURE_COLUMNS,
    MechanismParams,
    add_mechanism_features,
)


def _frame(n: int = 400, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 30.0, n)
    close = 30_000 + np.cumsum(steps)
    spread = np.abs(rng.normal(20.0, 12.0, n)) + 5.0
    high = close + spread
    low = close - spread
    open_ = close + rng.normal(0.0, 10.0, n)
    tr = np.maximum(high - low, 1.0)
    atr = pd.Series(tr).rolling(14).mean().to_numpy()
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
            "open": open_, "high": high, "low": low, "close": close,
            "volume": np.abs(rng.normal(100.0, 30.0, n)) + 1.0,
            "atr": atr,
        }
    )


def test_mechanism_features_are_causal_under_future_mutation():
    base = _frame()
    mutated = base.copy()
    # violently rewrite the future: row 300 must not notice
    mutated.loc[320:, ["open", "high", "low", "close"]] *= 1.5
    mutated.loc[320:, "volume"] *= 10.0

    params = MechanismParams()
    a = add_mechanism_features(base, params)
    b = add_mechanism_features(mutated, params)
    for col in MECHANISM_FEATURE_COLUMNS:
        va, vb = a[col].iloc[300], b[col].iloc[300]
        assert (pd.isna(va) and pd.isna(vb)) or va == vb, (
            f"{col} at row 300 changed when only future rows were mutated"
        )


def test_bars_since_features_saturate_instead_of_nan():
    # A monotone drift produces no sweeps and no engulfings: those features
    # must saturate at far_cap, never NaN, or they would purge every row.
    n = 300
    close = 30_000 + np.arange(n, dtype=float) * 5.0
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
            "open": close - 1.0, "high": close + 3.0, "low": close - 3.0,
            "close": close, "volume": np.full(n, 10.0),
            "atr": np.full(n, 6.0),
        }
    )
    params = MechanismParams()
    out = add_mechanism_features(df, params)
    tail = out.iloc[params.warmup_bars :]
    for col in (
        "bars_since_sweep_down", "bars_since_sweep_up",
        "bars_since_break_down", "dist_bull_fvg_atr", "dist_bear_fvg_atr",
    ):
        assert not tail[col].isna().any(), f"{col} went NaN on a quiet series"
        assert (tail[col] <= params.far_cap).all()


def test_feature_matrix_contract_includes_mechanism_columns():
    from vnedge.ml.feature_matrix import FEATURE_COLUMNS, FeatureParams, build_feature_matrix

    for col in MECHANISM_FEATURE_COLUMNS:
        assert col in FEATURE_COLUMNS
    df = _frame(600)
    out = build_feature_matrix(df, None, FeatureParams())
    for col in MECHANISM_FEATURE_COLUMNS:
        assert col in out.columns
    # past the combined warmup, mechanism features are populated
    params = FeatureParams()
    tail = out.iloc[params.warmup_bars :]
    assert len(tail) > 0
    assert not tail["donchian_pos"].isna().all()
    assert set(tail["st_dir"].dropna().unique()) <= {1.0, -1.0}
