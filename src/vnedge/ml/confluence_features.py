"""Confluence features: classic-TA and crypto-native families, as model inputs.

The 2026 retail consensus ("combine complementary families: trend, momentum,
volume, volatility, structure, crypto-native") is incorporated the only way
this platform's evidence allows: every family becomes a FEATURE the
meta-labeler may weigh, never a rule stacked into an entry condition. The
indicator-corpus audits measured both failure modes this avoids — standalone
mechanisms deflate to noise (family DSR <= 0.016 across 39 cells), and
hand-stacked AND-filters collapse sample and edge together (the v2 trials).
Confluence is something to LEARN from outcomes, not to assert.

Families added here, chosen for what the existing matrix lacks:

* divergence — price at a lookback extreme while RSI/OBV is not (the
  multi-divergence scanner idea, expressed as flags);
* classic oscillator state — RSI(14), MACD histogram in bps, stochastic %K;
* participation — OBV z-score;
* crypto-native — Open Interest z-score/change and price-vs-OI divergence
  (the platform already ingests OI; until now nothing consumed it), plus
  funding already covered upstream;
* cross-sectional — relative return and strength z versus a benchmark
  (BTC), the residualization direction stage-4 review called for;
* session — weekend flag (24/7 markets thin out on weekends).

Optional inputs (open interest, benchmark closes) follow the funding_z
doctrine: absent data yields neutral values, never NaN purges. Every feature
at bar i uses rows 0..i only.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ConfluenceParams:
    rsi_window: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    stoch_window: int = 14
    obv_z_window: int = 48
    divergence_lookback: int = 48
    divergence_margin: float = 2.0   # RSI points above its own extreme
    oi_z_window: int = 48
    oi_change_window: int = 6
    rel_return_window: int = 24
    rel_z_window: int = 96

    @property
    def warmup_bars(self) -> int:
        return max(
            self.macd_slow + self.macd_signal,
            self.divergence_lookback,
            self.obv_z_window,
            self.rel_z_window,
        ) + 2


#: appended to feature_matrix.FEATURE_COLUMNS — order is part of the contract
CONFLUENCE_FEATURE_COLUMNS = [
    "rsi14", "macd_hist_bps", "stoch_k",
    "obv_z",
    "bull_div_rsi", "bear_div_rsi", "bull_div_obv", "bear_div_obv",
    "oi_z", "oi_change", "oi_price_div",
    "rel_ret", "rel_strength_z",
    "is_weekend",
]


def _rsi(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / window, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / window, adjust=False).mean()
    rs = gain / loss.where(loss > 0)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def _zscore(series: pd.Series, window: int) -> pd.Series:
    roll = series.rolling(window)
    std = roll.std()
    return ((series - roll.mean()) / std.where(std > 0)).mask(std == 0.0, 0.0)


def _divergence(
    price: pd.Series, oscillator: pd.Series, lookback: int, margin: float
) -> tuple[pd.Series, pd.Series]:
    """Flags: price at its lookback extreme while the oscillator is not.

    A deliberately simple, causal proxy for divergence — the model decides
    whether it carries information; no pivot bookkeeping to overfit.
    """
    price_low = price.rolling(lookback).min()
    price_high = price.rolling(lookback).max()
    osc_low = oscillator.rolling(lookback).min()
    osc_high = oscillator.rolling(lookback).max()
    bull = (price <= price_low) & (oscillator > osc_low + margin)
    bear = (price >= price_high) & (oscillator < osc_high - margin)
    return bull.fillna(False).astype(float), bear.fillna(False).astype(float)


def add_confluence_features(
    df: pd.DataFrame,
    params: ConfluenceParams = ConfluenceParams(),
    *,
    open_interest: pd.Series | None = None,
    benchmark_close: pd.Series | None = None,
) -> pd.DataFrame:
    """Append CONFLUENCE_FEATURE_COLUMNS.

    ``open_interest`` and ``benchmark_close`` must be aligned to ``df``'s
    index when given; absent, their features are neutral (0.0), never NaN.
    """
    out = df.copy()
    close = out["close"]

    out["rsi14"] = _rsi(close, params.rsi_window)

    fast = close.ewm(span=params.macd_fast, adjust=False).mean()
    slow = close.ewm(span=params.macd_slow, adjust=False).mean()
    macd = fast - slow
    signal = macd.ewm(span=params.macd_signal, adjust=False).mean()
    out["macd_hist_bps"] = ((macd - signal) / close * 1e4)

    lowest = out["low"].rolling(params.stoch_window).min()
    highest = out["high"].rolling(params.stoch_window).max()
    span = (highest - lowest)
    out["stoch_k"] = (
        ((close - lowest) / span.where(span > 0)) * 100.0
    ).mask(span == 0.0, 50.0)

    direction = np.sign(close.diff()).fillna(0.0)
    obv = (direction * out["volume"]).cumsum()
    out["obv_z"] = _zscore(obv, params.obv_z_window)

    out["bull_div_rsi"], out["bear_div_rsi"] = _divergence(
        close, out["rsi14"], params.divergence_lookback, params.divergence_margin
    )
    # OBV divergence margin is in z-units on the same scale as obv_z.
    out["bull_div_obv"], out["bear_div_obv"] = _divergence(
        close, out["obv_z"], params.divergence_lookback, 0.25
    )

    if open_interest is not None:
        oi = pd.to_numeric(open_interest, errors="coerce").reindex(out.index).ffill()
        out["oi_z"] = _zscore(oi, params.oi_z_window).fillna(0.0)
        change = oi.pct_change(params.oi_change_window)
        out["oi_change"] = change.replace([np.inf, -np.inf], np.nan).fillna(0.0)
        price_up = close.pct_change(params.oi_change_window) > 0
        oi_down = change < 0
        # rising price on falling OI = short covering, not new conviction
        out["oi_price_div"] = (
            (price_up & oi_down).astype(float) - ((~price_up) & (~oi_down)).astype(float)
        ).where(oi.notna(), 0.0).fillna(0.0)
    else:
        out["oi_z"] = 0.0
        out["oi_change"] = 0.0
        out["oi_price_div"] = 0.0

    if benchmark_close is not None:
        bench = pd.to_numeric(benchmark_close, errors="coerce").reindex(out.index).ffill()
        own_ret = close.pct_change(params.rel_return_window)
        bench_ret = bench.pct_change(params.rel_return_window)
        rel = (own_ret - bench_ret).replace([np.inf, -np.inf], np.nan)
        out["rel_ret"] = rel.fillna(0.0)
        out["rel_strength_z"] = _zscore(rel.fillna(0.0), params.rel_z_window).fillna(0.0)
    else:
        out["rel_ret"] = 0.0
        out["rel_strength_z"] = 0.0

    day = pd.to_datetime(out["timestamp"], utc=True).dt.dayofweek
    out["is_weekend"] = (day >= 5).astype(float)

    return out
