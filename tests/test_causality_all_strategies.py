"""Repo-wide automated lookahead detection.

Every strategy in the registry is machine-checked for truncation invariance
on the deterministic synthetic market: re-running prepare()+signal() on a
truncated prefix must reproduce the full run's features and signals at every
overlapping index. Parametrizing over ``sorted(STRATEGIES)`` means any future
registration is covered automatically — no test change needed.

Canary tests prove the detector actually detects: a toy strategy using a
``shift(-1)`` feature and one peeking at ``index + 1`` inside ``signal()``
MUST both be flagged.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from vnedge.research.causality_analyzer import analyze_strategy, synthetic_market
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.strategy_registry import STRATEGIES

CANDLES, FUNDING = synthetic_market()


@pytest.mark.parametrize("strategy_id", sorted(STRATEGIES))
def test_registered_strategy_is_truncation_invariant(strategy_id: str) -> None:
    report = analyze_strategy(STRATEGIES[strategy_id], CANDLES, FUNDING)
    assert report.passed, report.describe()
    # Guard against a vacuous pass: the analysis must actually have compared
    # signal indexes and more than the raw OHLCV columns.
    assert report.signal_indexes_checked > 0
    assert len(report.feature_columns) > 5


def test_synthetic_market_is_deterministic_and_canonical() -> None:
    candles2, funding2 = synthetic_market()
    pd.testing.assert_frame_equal(CANDLES, candles2)
    pd.testing.assert_frame_equal(FUNDING, funding2)
    assert list(CANDLES.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert list(FUNDING.columns) == ["timestamp", "funding_rate"]
    assert isinstance(CANDLES["timestamp"].dtype, pd.DatetimeTZDtype)
    assert str(CANDLES["timestamp"].dt.tz) == "UTC"
    assert CANDLES["timestamp"].is_monotonic_increasing
    assert (CANDLES["high"] >= CANDLES[["open", "close"]].max(axis=1)).all()
    assert (CANDLES["low"] <= CANDLES[["open", "close"]].min(axis=1)).all()
    # Funding must contain trailing-percentile extremes (crowded positioning).
    assert FUNDING["funding_rate"].max() > 5e-4
    assert FUNDING["funding_rate"].min() < -5e-4


def test_signal_paths_are_exercised_not_just_features() -> None:
    """At least a majority of registered strategies must actually fire on the
    synthetic market — otherwise the signal comparison would silently degrade
    to None == None everywhere."""
    fired = {
        strategy_id: analyze_strategy(STRATEGIES[strategy_id], CANDLES, FUNDING).fired_bars
        for strategy_id in sorted(STRATEGIES)
    }
    firing = [strategy_id for strategy_id, bars in fired.items() if bars > 0]
    assert len(firing) >= len(STRATEGIES) // 2 + 1, f"too few strategies fire: {fired}"


# --- Canaries: the detector must detect ------------------------------------------


class _ShiftMinusOneCanary(BaseStrategy):
    """Classic lookahead: a shift(-1) feature leaks the NEXT bar's close."""

    strategy_id = "canary_shift_minus_one"
    warmup_bars = 5

    def __init__(self, funding: pd.DataFrame | None = None) -> None:
        self.funding = funding

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        df["next_close"] = df["close"].shift(-1)
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        next_close = float(row["next_close"])
        if not math.isnan(next_close) and next_close > float(row["close"]):
            return SignalIntent("long", stop_price=float(row["close"]) * 0.98)
        return None


class _PeekingSignalCanary(BaseStrategy):
    """Lookahead hidden in signal() itself: features are clean, but the
    decision reads row index + 1 when it exists."""

    strategy_id = "canary_peeking_signal"
    warmup_bars = 5

    def __init__(self, funding: pd.DataFrame | None = None) -> None:
        self.funding = funding

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return candles.copy()

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index + 1 < len(df) and float(df["close"].iloc[index + 1]) > float(
            df["close"].iloc[index]
        ):
            return SignalIntent("long", stop_price=float(df["close"].iloc[index]) * 0.98)
        return None


def test_canary_shift_minus_one_is_flagged() -> None:
    report = analyze_strategy(_ShiftMinusOneCanary, CANDLES, FUNDING)
    assert not report.passed
    # The leaked column is named in the evidence: full run has a value at the
    # boundary index, the truncated run has NaN there.
    assert any(
        v.kind == "feature" and v.field == "next_close" for v in report.violations
    ), report.describe()


def test_canary_peeking_signal_is_flagged() -> None:
    # Pick cut points whose boundary bar is followed by an up-move in the full
    # series — exactly where the peek changes the decision — so the test is
    # deterministic by construction, not by luck of the default cuts.
    closes = CANDLES["close"].to_numpy()
    cuts = [i for i in range(len(closes) // 2, len(closes)) if closes[i] > closes[i - 1]][:3]
    assert cuts, "synthetic market has no up-moves in its back half?"
    report = analyze_strategy(_PeekingSignalCanary, CANDLES, FUNDING, cut_points=cuts)
    assert not report.passed
    assert any(
        v.kind == "signal" and v.field == "fired" and v.index == v.cut - 1
        for v in report.violations
    ), report.describe()
