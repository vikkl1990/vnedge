"""Tests for the squeeze -> expansion breakout observer."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vnedge.strategy.squeeze_expansion_breakout import (
    PARAMS,
    SqueezeExpansionBreakout,
    SqueezeExpansionParams,
)


def _frame(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    n = len(closes)
    closes_arr = np.asarray(closes, dtype=float)
    spread = closes_arr * 0.0008
    return pd.DataFrame(
        {
            "open": closes_arr,
            "high": closes_arr + spread,
            "low": closes_arr - spread,
            "close": closes_arr,
            "volume": np.asarray(volumes, dtype=float) if volumes else np.full(n, 100.0),
        }
    )


def _squeeze_tape(expansion: bool, volume_confirm: bool = True) -> pd.DataFrame:
    """Long quiet tape whose final bars form a squeeze, then optionally break out."""
    rng = np.random.default_rng(7)
    warm = PARAMS.rank_lookback_bars + PARAMS.compression_bars + 10
    # noisy base regime so the trailing rank distribution has width
    base = 60_000 + np.cumsum(rng.normal(0, 28.0, warm))
    # compression tail: near-flat prices
    tail_len = PARAMS.compression_bars + 5
    tail = base[-1] + rng.normal(0, 1.5, tail_len)
    closes = np.concatenate([base, tail])
    volumes = np.full(len(closes), 100.0)
    if expansion:
        level = float(np.max(tail[-PARAMS.compression_bars :]))
        # clear the wick-based range high (close + spread) plus the buffer
        burst = level * (1 + 0.0008 + 12 / 10_000)
        closes = np.append(closes, burst)
        volumes = np.append(volumes, 250.0 if volume_confirm else 100.0)
    return _frame(closes.tolist(), volumes.tolist())


def test_fires_once_on_squeeze_expansion_with_volume() -> None:
    strat = SqueezeExpansionBreakout()
    df = strat.prepare(_squeeze_tape(expansion=True))
    idx = len(df) - 1
    intent = strat.signal(df, idx)
    assert intent is not None
    assert intent.side == "long"
    assert intent.stop_price < float(df.iloc[idx]["close"])
    assert intent.take_profit_price > float(df.iloc[idx]["close"])
    assert "virtual_only" in intent.reason


def test_silent_without_volume_confirmation() -> None:
    strat = SqueezeExpansionBreakout()
    df = strat.prepare(_squeeze_tape(expansion=True, volume_confirm=False))
    assert strat.signal(df, len(df) - 1) is None


def test_silent_without_compression() -> None:
    """The same breakout without a preceding squeeze must not fire."""
    rng = np.random.default_rng(11)
    warm = PARAMS.rank_lookback_bars + PARAMS.compression_bars + 10
    # persistently wide regime: every window ranks mid-distribution
    closes = 60_000 + np.cumsum(rng.normal(0, 30.0, warm))
    closes = np.append(closes, closes[-1] * (1 + 40 / 10_000))
    volumes = np.full(len(closes), 100.0)
    volumes[-1] = 250.0
    strat = SqueezeExpansionBreakout()
    df = strat.prepare(_frame(closes.tolist(), volumes.tolist()))
    assert strat.signal(df, len(df) - 1) is None


def test_one_fire_per_compression_episode() -> None:
    strat = SqueezeExpansionBreakout()
    base = _squeeze_tape(expansion=True)
    # extend with a second push above the same episode's range
    last = float(base.iloc[-1]["close"])
    extra = _frame([last * (1 + 4 / 10_000)], [300.0])
    df = strat.prepare(pd.concat([base, extra], ignore_index=True))
    fires = df["sqz_fire_long"].to_numpy() + df["sqz_fire_short"].to_numpy()
    assert fires.sum() <= 1.0


def test_params_are_frozen() -> None:
    with pytest.raises(ValueError, match="frozen"):
        SqueezeExpansionBreakout(params=SqueezeExpansionParams(compression_threshold=0.3))


def test_warmup_returns_none() -> None:
    strat = SqueezeExpansionBreakout()
    df = strat.prepare(_frame([60_000.0] * 60))
    assert strat.signal(df, 30) is None
