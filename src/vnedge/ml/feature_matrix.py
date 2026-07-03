"""Feature matrix builder — strictly causal features for ML models.

Every feature at bar i is computable from bars 0..i only (rolling windows and
backward shifts, reusing the same tested indicator utilities the rule-based
strategies use). NaN marks warmup, exactly as everywhere else in the system.
The causality property has a dedicated test: mutating future bars must not
change past feature rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from vnedge.strategy.indicators import (
    ema,
    rolling_percentile,
    sma,
    zscore,
)
from vnedge.strategy.regime import RegimeParams, add_regime_columns, merge_funding


@dataclass(frozen=True)
class FeatureParams:
    regime: RegimeParams = field(default_factory=RegimeParams)
    funding_pct_window: int = 240
    vol_window: int = 24
    z_window: int = 48

    @property
    def warmup_bars(self) -> int:
        from vnedge.strategy.regime import regime_warmup_bars

        return max(
            regime_warmup_bars(self.regime),
            self.funding_pct_window,
            self.z_window + 1,
        )


#: model input columns, in fixed order (order is part of the model contract)
FEATURE_COLUMNS = [
    "ret_1", "ret_6", "ret_24",
    "vol_24",
    "atr_pct", "er",
    "trend_atr", "dist_sma_atr",
    "funding_rate", "funding_pct",
    "volume_z", "range_atr", "close_z",
    "regime_up", "regime_down",
]


def build_feature_matrix(
    candles: pd.DataFrame,
    funding: pd.DataFrame | None,
    params: FeatureParams = FeatureParams(),
) -> pd.DataFrame:
    """Returns candles + regime columns + FEATURE_COLUMNS."""
    df = add_regime_columns(candles, params.regime)
    df = merge_funding(df, funding)
    close = df["close"]

    df["ret_1"] = close.pct_change(1)
    df["ret_6"] = close.pct_change(6)
    df["ret_24"] = close.pct_change(24)
    df["vol_24"] = df["ret_1"].rolling(params.vol_window).std()

    atr = df["atr"]
    fast = ema(close, params.regime.ema_fast)
    slow = ema(close, params.regime.ema_slow)
    df["trend_atr"] = (fast - slow) / atr
    df["dist_sma_atr"] = (close - sma(close, params.z_window)) / atr

    df["funding_pct"] = rolling_percentile(df["funding_rate"], params.funding_pct_window)
    df["volume_z"] = zscore(df["volume"], params.z_window)
    df["range_atr"] = (df["high"] - df["low"]) / atr
    df["close_z"] = zscore(close, params.z_window)
    df["regime_up"] = df["regime_trend_up"].astype(float)
    df["regime_down"] = df["regime_trend_down"].astype(float)
    return df
