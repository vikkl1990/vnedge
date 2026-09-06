from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vnedge.research.regime_stat_arb import (
    RegimeStatArbConfig,
    engle_granger_adf_t,
    filter_probability,
    fit_gaussian_hmm2,
    fit_pair_model,
    run_regime_stat_arb,
)


def _pair_frame(n: int = 720, *, seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    driver = np.cumsum(rng.normal(0, 0.004, n)) + np.log(2_500)
    residual = np.empty(n)
    residual[0] = 0
    for idx in range(1, n):
        residual[idx] = 0.82 * residual[idx - 1] + rng.normal(0, 0.006)
    log_b = driver
    log_a = 1.1 + 0.92 * log_b + residual
    close_a = np.exp(log_a)
    close_b = np.exp(log_b)
    open_a = close_a * np.exp(rng.normal(0, 0.0005, n))
    open_b = close_b * np.exp(rng.normal(0, 0.0005, n))
    a = pd.DataFrame({"timestamp": dates, "open": open_a, "close": close_a})
    b = pd.DataFrame({"timestamp": dates, "open": open_b, "close": close_b})
    return a, b


def test_hamilton_filter_is_causal_and_normalized() -> None:
    values = np.r_[np.full(100, -1.0), np.full(100, 1.0)]
    hmm = fit_gaussian_hmm2(values)
    p = filter_probability(-1.0, hmm.initial, hmm)
    assert sum(p) == pytest.approx(1.0)
    assert p[0] > p[1]
    p2 = filter_probability(1.0, p, hmm)
    assert sum(p2) == pytest.approx(1.0)


def test_pair_model_detects_stationary_residual() -> None:
    a, b = _pair_frame(1_000)
    model = fit_pair_model(a.close.to_numpy(), b.close.to_numpy())
    assert model.beta == pytest.approx(0.92, rel=0.08)
    assert model.adf_t < -3.34


def test_adf_rejects_random_walk_less_strongly() -> None:
    rng = np.random.default_rng(42)
    stationary = np.empty(1_000)
    stationary[0] = 0
    for idx in range(1, len(stationary)):
        stationary[idx] = 0.75 * stationary[idx - 1] + rng.normal()
    random_walk = np.cumsum(rng.normal(size=1_000))
    assert engle_granger_adf_t(stationary) < engle_granger_adf_t(random_walk)


def test_backtest_is_next_open_and_cost_fields_do_not_alias_gate() -> None:
    a, b = _pair_frame(900)
    result = run_regime_stat_arb(
        a,
        b,
        config=RegimeStatArbConfig(
            train_bars=300,
            test_bars=120,
            regime_probability=0.50,
            entry_z=0.8,
            min_net_edge_bps=0,
            execution_cost_bps=15.8,
            gate_cost_bps=18.8,
        ),
    )
    assert result.folds >= 1
    assert result.cointegrated_folds >= 1
    assert len(result.fold_diagnostics) == result.folds
    assert all(item.beta > 0 for item in result.fold_diagnostics)
    assert result.performance_eligible is False
    assert result.funding_included is False
    for trade in result.trades:
        assert trade.net_execution_bps == pytest.approx(trade.gross_bps - 15.8)
        assert trade.net_gate_bps == pytest.approx(trade.gross_bps - 18.8)
        assert pd.Timestamp(trade.exit_time) > pd.Timestamp(trade.entry_time)
