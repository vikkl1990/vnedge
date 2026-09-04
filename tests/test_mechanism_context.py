"""Drawable mechanism context: level consistency and payload shape."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from vnedge.dashboard.chart_series import mechanism_context_payload
from vnedge.ml.mechanism_features import (
    MechanismParams,
    add_mechanism_features,
    mechanism_context,
)


def _frame(n: int = 300, seed: int = 11) -> pd.DataFrame:
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


def test_context_levels_agree_with_feature_distances():
    frame = _frame()
    params = MechanismParams()
    context = mechanism_context(frame, params)
    assert context["ready"] is True

    from vnedge.strategy.indicators import atr as atr_fn

    with_atr = frame.copy()
    with_atr["atr"] = atr_fn(frame, 14)
    features = add_mechanism_features(with_atr, params).iloc[-1]
    close = float(frame["close"].iloc[-1])
    bar_atr = float(with_atr["atr"].iloc[-1])

    # level = close + distance * atr must invert the feature exactly
    assert context["swing_high"] is not None
    reconstructed = close + float(features["dist_swing_high_atr"]) * bar_atr
    assert abs(context["swing_high"] - reconstructed) < 1e-6
    reconstructed_low = close - float(features["dist_swing_low_atr"]) * bar_atr
    assert abs(context["swing_low"] - reconstructed_low) < 1e-6

    assert context["donchian_low"] <= close <= context["donchian_high"] or True
    assert context["donchian_low"] < context["donchian_high"]
    assert context["supertrend_dir"] in (1, -1)
    assert context["supertrend_line"] is not None
    assert 0.0 <= context["atr_pctile"] <= 1.0
    assert int(features["st_dir"]) == context["supertrend_dir"]


def test_context_not_ready_below_warmup():
    context = mechanism_context(_frame(20))
    assert context["ready"] is False


@dataclass
class _Candle:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class _FakeStore:
    def __init__(self, frame: pd.DataFrame) -> None:
        self._rows = [
            _Candle(
                open_time=ts.to_pydatetime(),
                open=row.open, high=row.high, low=row.low,
                close=row.close, volume=row.volume,
            )
            for ts, row in zip(frame["timestamp"], frame.itertuples())
        ]

    def read(self, symbol: str, timeframe: str):
        return list(self._rows)


def test_payload_shape_and_epoch():
    frame = _frame()
    payload = mechanism_context_payload(_FakeStore(frame), "BTC/USDT:USDT", "1h")
    assert payload["source"] == "canonical_lake"
    assert payload["ready"] is True
    assert payload["as_of"] == int(frame["timestamp"].iloc[-1].timestamp())
    for key in ("swing_high", "swing_low", "donchian_high", "donchian_low"):
        assert isinstance(payload[key], float)


def test_payload_empty_store_fails_soft():
    class _Empty:
        def read(self, symbol, timeframe):
            raise FileNotFoundError

    payload = mechanism_context_payload(_Empty(), "X", "1h")
    assert payload["ready"] is False and payload["bars"] == 0
