import numpy as np
import pandas as pd
import pytest

from vnedge.carry.backtest import evaluate, run_factor
from vnedge.carry.factor import CarryConfig, factor_pnl, funding_score, market_neutral_book


def _synth(n=220, syms=8, seed=0):
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    rng = np.random.default_rng(seed)
    close = pd.DataFrame(
        100 * np.cumprod(1 + rng.normal(0, 0.02, (n, syms)), axis=0),
        index=idx, columns=[f"S{i}" for i in range(syms)],
    )
    fund = pd.DataFrame(rng.normal(0, 0.001, (n, syms)), index=idx, columns=close.columns)
    return close, fund


def test_book_is_dollar_neutral():
    sc = pd.Series({"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6})
    w = market_neutral_book(sc, k=2)
    assert abs(w.sum()) < 1e-12                     # dollar-neutral
    assert (w > 0).sum() == 2 and (w < 0).sum() == 2
    assert w["A"] > 0 and w["F"] < 0                # long low-funding, short high


def test_book_rejects_overlap():
    with pytest.raises(ValueError):
        market_neutral_book(pd.Series({"A": 1, "B": 2, "C": 3}), k=2)  # 2*2 > 3


def test_funding_score_is_strictly_causal():
    close, fund = _synth()
    a = factor_pnl(close, fund, CarryConfig())
    fund2 = fund.copy()
    fund2.iloc[150] += 10.0                          # mutate a FUTURE print
    b = factor_pnl(close, fund2, CarryConfig())
    cutoff = a.index[80]                             # well before day 150
    pd.testing.assert_series_equal(a[a.index < cutoff], b[b.index < cutoff])


def test_cost_reduces_net_pnl():
    close, fund = _synth()
    free = factor_pnl(close, fund, CarryConfig(cost_bps=0.0)).sum()
    costed = factor_pnl(close, fund, CarryConfig(cost_bps=80.0)).sum()
    assert costed < free


def test_run_factor_stats_shape():
    close, fund = _synth()
    pnl, stats = run_factor(close, fund, CarryConfig())
    assert stats.n > 0 and isinstance(stats.sharpe, float)


@pytest.mark.skipif(
    not __import__("pathlib").Path("research/universe/BTC_1d.parquet").exists(),
    reason="universe parquet not present (fetched via research/universe_fetch.py)",
)
def test_regression_matches_sealed_tail_verdict():
    # guards the frozen xsect_carry_v1 numbers: seen promising, tail dead
    from vnedge.carry.universe import load_universe

    close, fund = load_universe("research/universe")
    cfg = CarryConfig()
    _, seen = run_factor(close, fund, cfg, end=pd.Timestamp("2025-06-30", tz="UTC"))
    _, tail = run_factor(close, fund, cfg,
                         start=pd.Timestamp("2025-07-01", tz="UTC"),
                         end=pd.Timestamp("2026-06-30", tz="UTC"))
    assert seen.sharpe == pytest.approx(0.80, abs=0.1)   # seen was promising
    assert tail.sharpe < 0.2                              # tail FAILED (was +0.01)
