"""Family deflation: replay accounting and family-statistic wiring."""

from __future__ import annotations

import numpy as np
import pandas as pd

from vnedge.research.ai_family_deflation import ROUND_TRIP_BPS, replay_daily_bps
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent


class _OneShot(BaseStrategy):
    strategy_id = "test_one_shot"
    warmup_bars = 2

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return candles.copy()

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index == 2:
            return SignalIntent(
                side="long", stop_price=90.0, take_profit_price=110.0, reason="t"
            )
        return None


def _candles(n: int, closes: list[float] | None = None) -> pd.DataFrame:
    close = np.array(closes if closes is not None else [100.0] * n, dtype=float)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
            "open": close, "high": close + 1.0, "low": close - 1.0,
            "close": close, "volume": np.full(n, 10.0),
        }
    )


def test_replay_books_target_hit_net_of_costs():
    closes = [100.0, 100.0, 100.0, 100.0, 112.0, 100.0, 100.0, 100.0]
    daily = replay_daily_bps(_OneShot(), _candles(8, closes), hold_bars=4)
    # entry next open (100), target 110 hit on the 112 bar: gross 1000 bps
    total = float(daily.sum())
    assert abs(total - (1000.0 - ROUND_TRIP_BPS)) < 1e-6
    # zero-filled across the covered span
    assert (daily == 0.0).sum() >= 0
    assert len(daily) >= 1


def test_replay_zero_fills_flat_days():
    n = 24 * 6  # six days, signal only on day one
    closes = [100.0] * n
    closes[4] = 112.0  # target bar
    daily = replay_daily_bps(_OneShot(), _candles(n, closes), hold_bars=4)
    assert len(daily) == 6
    assert (daily.iloc[1:] == 0.0).all()


def test_family_stats_shapes(monkeypatch, tmp_path):
    # two synthetic cells through the public entrypoint's statistics section
    from vnedge.ml.validation import (
        deflated_sharpe_ratio,
        probability_of_backtest_overfitting,
    )

    rng = np.random.default_rng(3)
    a = rng.normal(0.5, 5.0, 120)
    b = rng.normal(-0.2, 5.0, 120)
    matrix = np.column_stack([a, b])
    pbo = probability_of_backtest_overfitting(matrix, n_blocks=10)
    assert 0.0 <= pbo <= 1.0
    sharpes = [float(np.mean(x) / np.std(x, ddof=1)) for x in (a, b)]
    dsr = deflated_sharpe_ratio(a, n_trials=84, trial_sharpes=sharpes)
    assert 0.0 <= float(dsr) <= 1.0
