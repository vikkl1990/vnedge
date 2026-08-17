from __future__ import annotations

import pandas as pd
import pytest

from vnedge.strategy.fee_wall_momentum_observer import (
    FeeWallMomentumObserver,
    FeeWallMomentumParams,
)


def _flat_frame(rows: int = 310) -> pd.DataFrame:
    close = [100.0] * rows
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=rows, freq="5min", tz="UTC"),
            "open": close,
            "high": [100.05] * rows,
            "low": [99.95] * rows,
            "close": close,
            "volume": [100.0] * rows,
        }
    )


def test_fee_wall_cross_emits_virtual_long_with_bounded_sl_tp():
    candles = _flat_frame()
    candles.loc[300, ["high", "close", "volume"]] = [100.22, 100.20, 200.0]
    strategy = FeeWallMomentumObserver()
    prepared = strategy.prepare(candles)

    signal = strategy.signal(prepared, 300)

    assert signal is not None
    assert signal.side == "long"
    assert signal.stop_price == pytest.approx(100.20 * (1 - 25 / 10_000))
    assert signal.take_profit_price == pytest.approx(100.20 * (1 + 62.5 / 10_000))
    assert "horizon=5m" in signal.reason
    assert "fee_wall=13.0bps" in signal.reason
    assert "virtual_only" in signal.reason


def test_fee_wall_observer_does_not_repeat_same_direction_episode():
    candles = _flat_frame()
    candles.loc[300, ["high", "close", "volume"]] = [100.22, 100.20, 200.0]
    candles.loc[301, ["high", "low", "close", "volume"]] = [100.23, 100.19, 100.21, 150.0]
    strategy = FeeWallMomentumObserver()
    prepared = strategy.prepare(candles)

    assert strategy.signal(prepared, 300) is not None
    assert strategy.signal(prepared, 301) is None


def test_fee_wall_observer_rejects_thin_crossing():
    candles = _flat_frame()
    candles.loc[300, ["high", "close", "volume"]] = [100.22, 100.20, 10.0]
    strategy = FeeWallMomentumObserver()

    assert strategy.signal(strategy.prepare(candles), 300) is None


def test_fee_wall_features_are_prefix_causal():
    candles = _flat_frame(340)
    candles.loc[300:, "close"] = 100.25
    strategy = FeeWallMomentumObserver()
    prefix = strategy.prepare(candles.iloc[:310]).reset_index(drop=True)
    full = strategy.prepare(candles).iloc[:310].reset_index(drop=True)

    pd.testing.assert_frame_equal(prefix, full)


def test_fee_wall_registration_is_frozen():
    with pytest.raises(ValueError, match="frozen"):
        FeeWallMomentumObserver(
            params=FeeWallMomentumParams(fee_wall_bps=14.0)
        )
