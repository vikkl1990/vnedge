"""Tests for the regime detection router."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vnedge.strategy.regime_router import (
    DEFAULT_CONFIG,
    FAST_SQUEEZE,
    SLOW_PULLBACK,
    SLOW_RECLAIM,
    Regime,
    RegimeRouter,
    RegimeRouterConfig,
    annotate_with_hysteresis,
    build_policy,
)

CALM = {"vr": 0.8, "er": 0.2, "bar_range_bps": 20.0, "atr_pct_rank": 0.3}
TRENDY = {"vr": 1.3, "er": 0.5, "bar_range_bps": 30.0, "atr_pct_rank": 0.4}


def test_stress_blocks_every_sleeve_and_enters_immediately() -> None:
    router = RegimeRouter()
    for _ in range(5):
        router.update(**CALM)
    assert router.regime is Regime.RANGE
    # a blowout bar must flip the label on the same bar, not after hysteresis
    assert router.update(vr=1.0, er=0.2, bar_range_bps=200.0, atr_pct_rank=0.5) is Regime.STRESS
    assert router.allowed_sleeves() == frozenset()


def test_stress_also_triggers_on_atr_percentile() -> None:
    router = RegimeRouter()
    assert router.update(vr=1.0, er=0.2, bar_range_bps=20.0, atr_pct_rank=0.95) is Regime.STRESS


def test_expand_allows_slow_sleeves() -> None:
    router = RegimeRouter()
    for _ in range(4):
        router.update(**TRENDY)
    assert router.regime is Regime.EXPAND
    assert SLOW_RECLAIM in router.allowed_sleeves()
    assert SLOW_PULLBACK in router.allowed_sleeves()
    assert FAST_SQUEEZE in router.allowed_sleeves()


def test_range_is_fast_only_by_default() -> None:
    router = RegimeRouter()
    for _ in range(5):
        router.update(**CALM)
    assert router.regime is Regime.RANGE
    assert FAST_SQUEEZE in router.allowed_sleeves()
    assert SLOW_RECLAIM not in router.allowed_sleeves()


def test_unknown_features_fail_closed() -> None:
    router = RegimeRouter()
    for _ in range(4):
        router.update(**TRENDY)
    assert router.update(vr=float("nan"), er=0.5, bar_range_bps=30.0, atr_pct_rank=0.4) is (
        Regime.UNKNOWN
    )
    assert router.allowed_sleeves() == frozenset()


def test_hysteresis_delays_leaving_a_regime() -> None:
    router = RegimeRouter(config=RegimeRouterConfig(hysteresis_bars=3))
    for _ in range(4):
        router.update(**CALM)
    assert router.regime is Regime.RANGE
    # a single trendy bar must not flip the label
    assert router.update(**TRENDY) is Regime.RANGE
    assert router.update(**TRENDY) is Regime.RANGE
    assert router.update(**TRENDY) is Regime.EXPAND


def test_policy_respects_config_switches() -> None:
    policy = build_policy(RegimeRouterConfig(allow_slow_in_range=True))
    assert SLOW_RECLAIM in policy[Regime.RANGE]
    assert build_policy(DEFAULT_CONFIG)[Regime.STRESS] == frozenset()


def test_annotate_is_causal_under_truncation() -> None:
    rng = np.random.default_rng(4)
    n = 400
    close = 60_000 + np.cumsum(rng.normal(0, 40.0, n))
    frame = pd.DataFrame(
        {"high": close + 25, "low": close - 25, "close": close}
    )
    full = annotate_with_hysteresis(frame)
    cut = 320
    prefix = annotate_with_hysteresis(frame.iloc[:cut].copy())
    for column in ("regime_vr", "regime_er", "regime_bar_range_bps", "regime"):
        a = full[column].iloc[:cut].reset_index(drop=True)
        b = prefix[column].reset_index(drop=True)
        if a.dtype.kind == "f":
            pd.testing.assert_series_equal(a, b, check_names=False, rtol=1e-12)
        else:
            # regime label depends only on bars at or before each index
            assert list(a) == list(b), column


def test_annotate_marks_warmup_unknown_and_denies() -> None:
    n = 120
    frame = pd.DataFrame(
        {"high": np.full(n, 101.0), "low": np.full(n, 99.0), "close": np.full(n, 100.0)}
    )
    out = annotate_with_hysteresis(frame)
    assert (out["regime"].iloc[: DEFAULT_CONFIG.min_bars] == Regime.UNKNOWN.value).all()
    assert not out["regime_allows_fast"].iloc[: DEFAULT_CONFIG.min_bars].any()
    assert not out["regime_allows_slow"].iloc[: DEFAULT_CONFIG.min_bars].any()


def test_invalid_config_is_refused() -> None:
    with pytest.raises(ValueError):
        RegimeRouterConfig(vr_expand=0.5, vr_range=0.9)
    with pytest.raises(ValueError):
        RegimeRouterConfig(atr_short=48, atr_long=12)
