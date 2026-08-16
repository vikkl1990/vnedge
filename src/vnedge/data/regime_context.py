"""Causal market-regime measurements for closed canonical candles.

This module is deliberately below strategy and execution.  It describes the
latest closed-bar environment; it cannot emit an intent, size a position, mark
a strategy tradeable, or bypass CostGate/registry controls.

Every value is calculated from the supplied prefix only.  Forming bars,
non-consecutive series, mixed symbols/timeframes, and degraded input fail
closed with an explicit, operator-visible reason.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from itertools import pairwise
from math import isfinite
from typing import Literal

import pandas as pd

from vnedge.data.candles import TF_SECONDS, Candle


class RegimeLabel(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    HIGH_VOLATILITY = "high_volatility"
    LOW_LIQUIDITY = "low_liquidity"
    MEAN_REVERSION = "mean_reversion"
    SIDEWAYS = "sideways"
    UNAVAILABLE = "unavailable"


TrendDirection = Literal["up", "down", "flat"]
DataQuality = Literal["ok", "degraded", "gap"]


@dataclass(frozen=True, slots=True)
class RegimeConfig:
    """Pre-registered thresholds shared by 1h and 4h measurements."""

    adx_period: int = 14
    ema_period: int = 30
    ema_slope_bars: int = 5
    bb_period: int = 20
    percentile_window: int = 20
    volume_window: int = 20
    trend_adx_min: float = 25.0
    trend_slope_bps_min: float = 2.0
    high_vol_percentile: float = 0.90
    low_liquidity_ratio: float = 0.50
    mean_reversion_adx_max: float = 18.0
    squeeze_percentile_max: float = 0.30

    def __post_init__(self) -> None:
        periods = (
            self.adx_period,
            self.ema_period,
            self.ema_slope_bars,
            self.bb_period,
            self.percentile_window,
            self.volume_window,
        )
        if any(value < 2 for value in periods):
            raise ValueError("regime periods must be at least two bars")
        unit_values = (
            self.high_vol_percentile,
            self.low_liquidity_ratio,
            self.squeeze_percentile_max,
        )
        if any(value < 0 or value > 1 for value in unit_values):
            raise ValueError("regime percentile/ratio thresholds must be in [0, 1]")
        if self.trend_adx_min <= 0 or self.trend_slope_bps_min < 0:
            raise ValueError("trend thresholds must be non-negative")

    @property
    def warmup_bars(self) -> int:
        return max(
            2 * self.adx_period,
            self.ema_period + self.ema_slope_bars,
            self.bb_period + self.percentile_window - 1,
            self.adx_period + self.percentile_window - 1,
            self.volume_window,
        )


REGIME_CONFIG = RegimeConfig()


@dataclass(frozen=True, slots=True)
class RegimeContext:
    as_of: datetime | None
    symbol: str
    timeframe: str
    label: RegimeLabel
    trend_direction: TrendDirection
    adx: float | None
    atr_percentile: float | None
    ema_slope_bps: float | None
    bb_width_bps: float | None
    bb_width_percentile: float | None
    volume_ratio: float | None
    confidence: float
    data_quality: DataQuality
    ready: bool
    reason: str


def _unavailable(
    bars: Sequence[Candle],
    quality: DataQuality,
    reason: str,
) -> RegimeContext:
    last = bars[-1] if bars else None
    return RegimeContext(
        as_of=last.close_time if last is not None else None,
        symbol=last.symbol if last is not None else "",
        timeframe=last.timeframe if last is not None else "",
        label=RegimeLabel.UNAVAILABLE,
        trend_direction="flat",
        adx=None,
        atr_percentile=None,
        ema_slope_bps=None,
        bb_width_bps=None,
        bb_width_percentile=None,
        volume_ratio=None,
        confidence=0.0,
        data_quality=quality,
        ready=False,
        reason=reason,
    )


def _last_percentile(values: pd.Series, window: int) -> float | None:
    sample = values.dropna().iloc[-window:]
    if len(sample) < window:
        return None
    current = float(sample.iloc[-1])
    below = float((sample < current).sum())
    equal = float((sample == current).sum())
    return (below + 0.5 * equal) / float(window)


def _adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int
) -> tuple[pd.Series, pd.Series]:
    """Return causal true-range average and Wilder-style ADX series."""
    prior_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prior_close).abs(), (low - prior_close).abs()], axis=1
    ).max(axis=1)
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    alpha = 1.0 / float(period)
    atr = true_range.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_smoothed = plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    minus_smoothed = minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * plus_smoothed / atr.replace(0, float("nan"))
    minus_di = 100.0 * minus_smoothed / atr.replace(0, float("nan"))
    denominator = (plus_di + minus_di).replace(0, float("nan"))
    dx = 100.0 * (plus_di - minus_di).abs() / denominator
    adx = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    return atr, adx


def detect_regime(
    bars: Sequence[Candle],
    *,
    data_quality: DataQuality = "ok",
    config: RegimeConfig = REGIME_CONFIG,
) -> RegimeContext:
    """Measure the regime at the final closed candle in ``bars``.

    The caller must pass an unbroken canonical series ending at the evaluation
    boundary.  The function never repairs missing periods or uses forming data.
    """
    if data_quality != "ok":
        return _unavailable(bars, data_quality, f"data_quality_{data_quality}")
    if len(bars) < config.warmup_bars:
        return _unavailable(bars, data_quality, "insufficient_history")

    first = bars[0]
    if any(not bar.is_closed for bar in bars):
        return _unavailable(bars, data_quality, "forming_bar_present")
    if any(bar.symbol != first.symbol for bar in bars):
        return _unavailable(bars, data_quality, "mixed_symbols")
    if any(bar.timeframe != first.timeframe for bar in bars):
        return _unavailable(bars, data_quality, "mixed_timeframes")
    step = TF_SECONDS[first.timeframe]
    if any(
        int((right.open_time - left.open_time).total_seconds()) != step
        for left, right in pairwise(bars)
    ):
        return _unavailable(bars, "gap", "non_consecutive_candles")

    frame = pd.DataFrame(
        {
            "high": [float(bar.high) for bar in bars],
            "low": [float(bar.low) for bar in bars],
            "close": [float(bar.close) for bar in bars],
            "volume": [float(bar.volume) for bar in bars],
        }
    )
    atr, adx_series = _adx(frame["high"], frame["low"], frame["close"], config.adx_period)
    atr_percentile = _last_percentile(atr, config.percentile_window)
    adx = float(adx_series.iloc[-1])

    ema = frame["close"].ewm(span=config.ema_period, adjust=False).mean()
    previous_ema = float(ema.iloc[-1 - config.ema_slope_bars])
    ema_slope_bps = (
        (float(ema.iloc[-1]) / previous_ema - 1.0) * 10_000.0 / float(config.ema_slope_bars)
    )
    trend_direction: TrendDirection = (
        "up" if ema_slope_bps > 0 else "down" if ema_slope_bps < 0 else "flat"
    )

    middle = frame["close"].rolling(config.bb_period).mean()
    deviation = frame["close"].rolling(config.bb_period).std(ddof=0)
    bb_width = 4.0 * deviation / middle.replace(0, float("nan")) * 10_000.0
    bb_width_bps = float(bb_width.iloc[-1])
    bb_percentile = _last_percentile(bb_width, config.percentile_window)
    volume_mean = float(frame["volume"].iloc[-config.volume_window :].mean())
    volume_ratio = float(frame["volume"].iloc[-1]) / volume_mean if volume_mean > 0 else 0.0

    measurements = (adx, atr_percentile, ema_slope_bps, bb_width_bps, bb_percentile, volume_ratio)
    if any(value is None or not isfinite(float(value)) for value in measurements):
        return _unavailable(bars, data_quality, "non_finite_measurement")

    assert atr_percentile is not None and bb_percentile is not None
    trending = adx >= config.trend_adx_min and abs(ema_slope_bps) >= config.trend_slope_bps_min
    if volume_ratio < config.low_liquidity_ratio:
        label = RegimeLabel.LOW_LIQUIDITY
        confidence = min(
            1.0, (config.low_liquidity_ratio - volume_ratio) / max(config.low_liquidity_ratio, 0.01)
        )
    elif atr_percentile >= config.high_vol_percentile:
        label = RegimeLabel.HIGH_VOLATILITY
        confidence = atr_percentile
    elif trending:
        label = RegimeLabel.TRENDING_UP if trend_direction == "up" else RegimeLabel.TRENDING_DOWN
        confidence = min(1.0, adx / 50.0)
    elif adx <= config.mean_reversion_adx_max and bb_percentile <= config.squeeze_percentile_max:
        label = RegimeLabel.MEAN_REVERSION
        confidence = min(1.0, 1.0 - adx / max(config.mean_reversion_adx_max, 0.01))
    else:
        label = RegimeLabel.SIDEWAYS
        confidence = max(0.0, min(1.0, 1.0 - adx / max(config.trend_adx_min, 0.01)))

    return RegimeContext(
        as_of=bars[-1].close_time,
        symbol=first.symbol,
        timeframe=first.timeframe,
        label=label,
        trend_direction=trend_direction,
        adx=adx,
        atr_percentile=atr_percentile,
        ema_slope_bps=ema_slope_bps,
        bb_width_bps=bb_width_bps,
        bb_width_percentile=bb_percentile,
        volume_ratio=volume_ratio,
        confidence=confidence,
        data_quality=data_quality,
        ready=True,
        reason="ok",
    )


__all__ = [
    "REGIME_CONFIG",
    "RegimeConfig",
    "RegimeContext",
    "RegimeLabel",
    "detect_regime",
]
