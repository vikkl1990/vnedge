"""Deterministic, causal regime and candle-feature calculations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean, pstdev

from vnedge.scalping.delta_engine.types import Candle, Regime


def _ema(values: list[float], span: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (span + 1)
    out = values[0]
    for value in values[1:]:
        out = alpha * value + (1 - alpha) * out
    return out


def _true_ranges(rows: tuple[Candle, ...]) -> list[float]:
    out: list[float] = []
    for idx, candle in enumerate(rows):
        previous = rows[idx - 1].close if idx else candle.open
        out.append(max(candle.high - candle.low, abs(candle.high - previous), abs(candle.low - previous)))
    return out


def _efficiency(closes: list[float], window: int) -> float:
    sample = closes[-(window + 1) :]
    if len(sample) < window + 1:
        return 0.0
    path = sum(abs(sample[i] - sample[i - 1]) for i in range(1, len(sample)))
    return abs(sample[-1] - sample[0]) / path if path else 0.0


@dataclass(frozen=True)
class RegimeConfig:
    fast_ema: int = 12
    slow_ema: int = 36
    efficiency_window: int = 12
    trend_efficiency_min: float = 0.28
    expansion_ratio: float = 1.35
    funding_extreme_abs: float = 0.0005


class RegimeEngine:
    def __init__(self, config: RegimeConfig | None = None) -> None:
        self.config = config or RegimeConfig()

    def classify(
        self,
        candles: dict[str, tuple[Candle, ...]],
        *,
        funding_rate: float,
    ) -> Regime:
        if abs(funding_rate) >= self.config.funding_extreme_abs:
            return Regime.FUNDING_EXTREME
        hourly = candles.get("1h", ())
        intraday = candles.get("15m", ())
        if len(hourly) < self.config.slow_ema or len(intraday) < 24:
            return Regime.UNKNOWN
        ranges = _true_ranges(intraday)
        recent_atr = fmean(ranges[-6:])
        baseline_atr = fmean(ranges[-24:-6]) if ranges[-24:-6] else recent_atr
        if baseline_atr > 0 and recent_atr / baseline_atr >= self.config.expansion_ratio:
            return Regime.EXPANDING
        closes = [row.close for row in hourly]
        fast = _ema(closes[-self.config.slow_ema :], self.config.fast_ema)
        slow = _ema(closes[-self.config.slow_ema :], self.config.slow_ema)
        efficiency = _efficiency(closes, self.config.efficiency_window)
        if efficiency >= self.config.trend_efficiency_min:
            if fast > slow:
                return Regime.TRENDING_UP
            if fast < slow:
                return Regime.TRENDING_DOWN
        return Regime.QUIET


def build_features(candles: dict[str, tuple[Candle, ...]]) -> dict[str, float]:
    """One feature definition shared by live context and replay."""
    rows = candles.get("1m", ()) or candles.get("5m", ())
    if not rows:
        return {}
    latest = rows[-1]
    previous = rows[:-1]
    close = latest.close
    candle_range = max(latest.range, close * 1e-9)
    body = latest.close - latest.open
    upper_wick = latest.high - max(latest.open, latest.close)
    lower_wick = min(latest.open, latest.close) - latest.low
    returns = [
        math.log(rows[i].close / rows[i - 1].close)
        for i in range(1, len(rows))
        if rows[i - 1].close > 0
    ]
    volumes = [row.volume for row in previous[-30:]]
    mean_volume = fmean(volumes) if volumes else latest.volume
    volume_std = pstdev(volumes) if len(volumes) > 1 else 0.0
    trs = _true_ranges(rows[-20:])
    atr = fmean(trs[-14:]) if trs else latest.range
    recent = previous[-12:]
    high_break = max((row.high for row in recent), default=latest.high)
    low_break = min((row.low for row in recent), default=latest.low)
    gains = [max(0.0, returns[i]) for i in range(max(0, len(returns) - 14), len(returns))]
    losses = [max(0.0, -returns[i]) for i in range(max(0, len(returns) - 14), len(returns))]
    avg_gain = fmean(gains) if gains else 0.0
    avg_loss = fmean(losses) if losses else 0.0
    rsi = 100.0 if avg_loss == 0 and avg_gain > 0 else (
        50.0 if avg_loss == avg_gain == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    )
    closes = [row.close for row in rows]
    return {
        "return_1_bps": (latest.close / rows[-2].close - 1) * 10_000 if len(rows) > 1 else 0.0,
        "return_5_bps": (latest.close / rows[-6].close - 1) * 10_000 if len(rows) > 5 else 0.0,
        "atr_bps": atr / close * 10_000,
        "realized_vol_bps": pstdev(returns[-20:]) * 10_000 if len(returns) > 1 else 0.0,
        "body_ratio": abs(body) / candle_range,
        "body_direction": 1.0 if body > 0 else -1.0 if body < 0 else 0.0,
        "upper_wick_ratio": upper_wick / candle_range,
        "lower_wick_ratio": lower_wick / candle_range,
        "volume_z": (latest.volume - mean_volume) / volume_std if volume_std else 0.0,
        "prior_high_12": high_break,
        "prior_low_12": low_break,
        "breakout_up_bps": (latest.close / high_break - 1) * 10_000,
        "breakout_down_bps": (low_break / latest.close - 1) * 10_000,
        "ema_gap_bps": (_ema(closes[-30:], 9) / _ema(closes[-30:], 21) - 1) * 10_000,
        "rsi_14": rsi,
    }
