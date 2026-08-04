"""Causal MTF level-rejection scanner with Adaptive Momentum Fusion.

This is an observation tool, not a strategy or execution route.  It combines
completed 1h/4h range levels with a causal port of the Efficiency-engine AMF
histogram and emits research alerts only.  It is deliberately absent from the
strategy registry.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

import numpy as np
import pandas as pd

from vnedge.strategy.indicators import atr, prior_high, prior_low

SCANNER_ID = "mtf_amf_rejection_scanner_v1"
DEFAULT_OUT = Path("research/live_research/mtf_amf_rejection_scanner_latest.json")
Side = Literal["long", "short"]


@dataclass(frozen=True)
class MtfAmfScannerConfig:
    chart_timeframe: str = "1h"
    higher_timeframe: str = "4h"
    range_lookback: int = 20
    atr_window: int = 14
    max_level_distance_atr: float = 0.10
    fast_length: int = 8
    slow_length: int = 21
    signal_length: int = 7
    jitter_reduction: float = 0.70
    ranging_regime_max: float = 0.50
    warmup_bars: int = 50
    cooldown_bars: int = 62
    observation_horizons: tuple[int, ...] = (15, 40, 62)
    assumed_round_trip_cost_bps: float = 12.50

    def __post_init__(self) -> None:
        if self.chart_timeframe != "1h" or self.higher_timeframe != "4h":
            raise ValueError("this evidence-locked scanner supports 1h + completed 4h only")
        if self.range_lookback < 2:
            raise ValueError("range_lookback must be >= 2")
        if not 0 < self.max_level_distance_atr <= 1:
            raise ValueError("max_level_distance_atr must be in (0, 1]")
        if self.fast_length >= self.slow_length:
            raise ValueError("fast_length must be less than slow_length")
        if not 0 <= self.jitter_reduction <= 1:
            raise ValueError("jitter_reduction must be in [0, 1]")
        if self.cooldown_bars < 0:
            raise ValueError("cooldown_bars must be >= 0")
        if not self.observation_horizons or min(self.observation_horizons) < 1:
            raise ValueError("observation_horizons must contain positive bars")


DEFAULT_CONFIG = MtfAmfScannerConfig()


@dataclass(frozen=True)
class MtfAmfAlert:
    scanner_id: str
    symbol: str
    bar_start: str
    observed_at: str
    side: Side
    level: float
    one_hour_level: float
    four_hour_level: float
    level_distance_atr: float
    atr: float
    close: float
    amf_histogram: float
    amf_regime: float
    setup: str = "completed_mtf_level_rejection_with_amf_alignment"
    research_only: bool = True
    can_trade: bool = False
    can_promote: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ObservationSummary:
    horizon_bars: int
    resolved: int
    avg_gross_bps: float | None
    avg_after_cost_assumption_bps: float | None
    win_rate_pct: float | None
    profit_factor_after_cost: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def scan_mtf_amf_rejections(
    one_hour: pd.DataFrame,
    four_hour: pd.DataFrame,
    *,
    symbol: str,
    config: MtfAmfScannerConfig = DEFAULT_CONFIG,
) -> tuple[MtfAmfAlert, ...]:
    """Scan closed 1h bars using only levels known at each bar's start.

    The current 1h candle is excluded from its own range.  A 4h candle becomes
    eligible only at ``4h timestamp + 4 hours``.  These rules are stricter than
    merely setting TradingView ``lookahead_off`` and prevent forming HTF bars
    from moving a historical level.
    """

    frame = build_mtf_amf_feature_frame(one_hour, four_hour, config=config)
    alerts: list[MtfAmfAlert] = []
    blocked_until = -1
    for pos, row in frame.iterrows():
        if pos < blocked_until or pos < config.warmup_bars:
            continue
        alert = _alert_from_row(row, symbol=symbol, config=config)
        if alert is None:
            continue
        alerts.append(alert)
        blocked_until = pos + config.cooldown_bars + 1
    return tuple(alerts)


def build_mtf_amf_feature_frame(
    one_hour: pd.DataFrame,
    four_hour: pd.DataFrame,
    *,
    config: MtfAmfScannerConfig = DEFAULT_CONFIG,
) -> pd.DataFrame:
    """Return the causal feature frame used by the scanner."""

    one = _canonical_candles(one_hour, "one_hour")
    four = _canonical_candles(four_hour, "four_hour")

    one["one_hour_high"] = prior_high(one["high"], config.range_lookback)
    one["one_hour_low"] = prior_low(one["low"], config.range_lookback)
    one["atr"] = atr(one, config.atr_window)
    amf = _amf_features(one["close"], config)
    one = pd.concat([one, amf], axis=1)

    # A bar stamped 08:00 contains 08:00-12:00 activity.  Its completed range
    # values can only be joined to a 1h signal bar starting at/after 12:00.
    # The completed 4h candle may participate once its close time is reached;
    # unlike the still-forming 1h signal candle, it is fully known then.
    four["four_hour_high"] = four["high"].rolling(config.range_lookback).max()
    four["four_hour_low"] = four["low"].rolling(config.range_lookback).min()
    completed = four[["timestamp", "four_hour_high", "four_hour_low"]].copy()
    completed["available_at"] = completed["timestamp"] + pd.Timedelta(hours=4)
    completed = completed.drop(columns="timestamp").dropna().sort_values("available_at")

    merged = pd.merge_asof(
        one.sort_values("timestamp"),
        completed,
        left_on="timestamp",
        right_on="available_at",
        direction="backward",
        allow_exact_matches=True,
    )
    merged["upper_distance_atr"] = (
        merged["one_hour_high"] - merged["four_hour_high"]
    ).abs() / merged["atr"]
    merged["lower_distance_atr"] = (
        merged["one_hour_low"] - merged["four_hour_low"]
    ).abs() / merged["atr"]
    merged["upper_level"] = (merged["one_hour_high"] + merged["four_hour_high"]) / 2.0
    merged["lower_level"] = (merged["one_hour_low"] + merged["four_hour_low"]) / 2.0
    return merged.reset_index(drop=True)


def summarize_observations(
    one_hour: pd.DataFrame,
    alerts: Iterable[MtfAmfAlert],
    *,
    config: MtfAmfScannerConfig = DEFAULT_CONFIG,
) -> tuple[ObservationSummary, ...]:
    """Score fixed-horizon observations; this is not an executable P&L model."""

    candles = _canonical_candles(one_hour, "one_hour")
    positions = {ts.isoformat(): pos for pos, ts in enumerate(candles["timestamp"])}
    alert_rows = tuple(alerts)
    summaries: list[ObservationSummary] = []
    for horizon in config.observation_horizons:
        returns: list[float] = []
        for alert in alert_rows:
            pos = positions.get(pd.Timestamp(alert.bar_start).isoformat())
            entry_pos = pos + 1 if pos is not None else None
            exit_pos = entry_pos + horizon - 1 if entry_pos is not None else None
            if entry_pos is None or exit_pos is None or exit_pos >= len(candles):
                continue
            entry = float(candles.iloc[entry_pos]["open"])
            future = float(candles.iloc[exit_pos]["close"])
            direction = 1.0 if alert.side == "long" else -1.0
            returns.append(direction * (future / entry - 1.0) * 10_000.0)
        net = [value - config.assumed_round_trip_cost_bps for value in returns]
        summaries.append(
            ObservationSummary(
                horizon_bars=horizon,
                resolved=len(returns),
                avg_gross_bps=_mean_or_none(returns),
                avg_after_cost_assumption_bps=_mean_or_none(net),
                win_rate_pct=(100.0 * sum(value > 0 for value in net) / len(net) if net else None),
                profit_factor_after_cost=_profit_factor(net),
            )
        )
    return tuple(summaries)


def build_scanner_payload(
    one_hour: pd.DataFrame,
    four_hour: pd.DataFrame,
    *,
    symbol: str,
    config: MtfAmfScannerConfig = DEFAULT_CONFIG,
    now: datetime | None = None,
) -> dict[str, Any]:
    alerts = scan_mtf_amf_rejections(one_hour, four_hour, symbol=symbol, config=config)
    observations = summarize_observations(one_hour, alerts, config=config)
    return {
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "scanner_id": SCANNER_ID,
        "symbol": symbol,
        "mode": "historical_and_latest_observation_only",
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "registered_strategy": False,
            "order_route_present": False,
            "bracket_exit_status": "failed_audit_not_implemented",
            "observation_fill_model": "next_open_to_fixed_horizon_close",
            "requires_untouched_judgment": True,
        },
        "config": asdict(config),
        "summary": {
            "alerts": len(alerts),
            "latest_alert": alerts[-1].to_dict() if alerts else None,
            "fixed_horizon_observations": [row.to_dict() for row in observations],
        },
        "alerts": [alert.to_dict() for alert in alerts],
        "can_trade": False,
        "can_promote": False,
    }


def publish_scanner_payload(payload: dict[str, Any], out: Path | str) -> Path:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", dir=path.parent, prefix=path.name, suffix=".tmp", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        tmp = Path(handle.name)
    tmp.replace(path)
    return path


def _alert_from_row(
    row: pd.Series,
    *,
    symbol: str,
    config: MtfAmfScannerConfig,
) -> MtfAmfAlert | None:
    required = (
        "atr",
        "amf_histogram",
        "amf_regime",
        "one_hour_high",
        "one_hour_low",
        "four_hour_high",
        "four_hour_low",
    )
    if any(pd.isna(row[name]) for name in required):
        return None
    if float(row["atr"]) <= 0 or float(row["amf_regime"]) >= config.ranging_regime_max:
        return None

    upper_ok = float(row["upper_distance_atr"]) <= config.max_level_distance_atr
    lower_ok = float(row["lower_distance_atr"]) <= config.max_level_distance_atr
    short_rejection = (
        upper_ok
        and float(row["high"]) >= float(row["upper_level"])
        and float(row["close"]) < float(row["upper_level"])
        and float(row["amf_histogram"]) < 0
    )
    long_rejection = (
        lower_ok
        and float(row["low"]) <= float(row["lower_level"])
        and float(row["close"]) > float(row["lower_level"])
        and float(row["amf_histogram"]) > 0
    )
    if short_rejection:
        side: Side = "short"
        level = float(row["upper_level"])
        one_level = float(row["one_hour_high"])
        four_level = float(row["four_hour_high"])
        distance = float(row["upper_distance_atr"])
    elif long_rejection:
        side = "long"
        level = float(row["lower_level"])
        one_level = float(row["one_hour_low"])
        four_level = float(row["four_hour_low"])
        distance = float(row["lower_distance_atr"])
    else:
        return None

    bar_start = pd.Timestamp(row["timestamp"])
    observed_at = bar_start + pd.Timedelta(hours=1)
    return MtfAmfAlert(
        scanner_id=SCANNER_ID,
        symbol=symbol,
        bar_start=bar_start.isoformat(),
        observed_at=observed_at.isoformat(),
        side=side,
        level=level,
        one_hour_level=one_level,
        four_hour_level=four_level,
        level_distance_atr=distance,
        atr=float(row["atr"]),
        close=float(row["close"]),
        amf_histogram=float(row["amf_histogram"]),
        amf_regime=float(row["amf_regime"]),
    )


def _amf_features(close: pd.Series, config: MtfAmfScannerConfig) -> pd.DataFrame:
    values = close.to_numpy(dtype=float)
    fast = _efficiency_adaptive_ema(values, config.fast_length)
    slow = _efficiency_adaptive_ema(values, config.slow_length)
    oscillator = fast - slow
    jurik = _jurik_smooth(oscillator, config.signal_length)
    smoothing = config.jitter_reduction * 0.85
    signal = np.empty_like(jurik)
    signal[0] = jurik[0]
    for idx in range(1, len(jurik)):
        signal[idx] = smoothing * signal[idx - 1] + (1.0 - smoothing) * jurik[idx]

    er = _efficiency_ratio(values, config.slow_length)
    regime = pd.Series(er).rolling(10).mean().fillna(pd.Series(er))
    regime = ((regime - 0.15) / 0.35).clip(0.0, 1.0)
    return pd.DataFrame(
        {
            "amf_oscillator": oscillator,
            "amf_signal": signal,
            "amf_histogram": oscillator - signal,
            "amf_regime": regime.to_numpy(dtype=float),
        }
    )


def _efficiency_ratio(values: np.ndarray, length: int) -> np.ndarray:
    result = np.full(len(values), 0.5, dtype=float)
    for idx in range(length, len(values)):
        path = float(np.abs(np.diff(values[idx - length : idx + 1])).sum())
        result[idx] = abs(values[idx] - values[idx - length]) / path if path else 0.5
    return result


def _efficiency_adaptive_ema(values: np.ndarray, length: int) -> np.ndarray:
    er = _efficiency_ratio(values, length)
    fast_sc = 2.0 / 3.0
    slow_sc = 2.0 / 31.0
    alpha = np.square(er * (fast_sc - slow_sc) + slow_sc)
    output = np.empty_like(values)
    output[0] = values[0]
    for idx in range(1, len(values)):
        safe_alpha = min(max(float(alpha[idx]), 0.01), 1.0)
        output[idx] = safe_alpha * values[idx] + (1.0 - safe_alpha) * output[idx - 1]
    return output


def _jurik_smooth(values: np.ndarray, length: int) -> np.ndarray:
    beta = 0.45 * (length - 1.0) / (0.45 * (length - 1.0) + 2.0)
    alpha = beta**3
    output = np.empty_like(values)
    e0 = float(values[0])
    e1 = 0.0
    e2 = 0.0
    output[0] = float(values[0])
    for idx in range(1, len(values)):
        source = float(values[idx])
        e0 = (1.0 - alpha) * source + alpha * e0
        e1 = (source - e0) * (1.0 - beta) + beta * e1
        e2 = (e0 + 1.5 * e1 - output[idx - 1]) * (1.0 - alpha) ** 2 + alpha**2 * e2
        output[idx] = output[idx - 1] + e2
    return output


def _canonical_candles(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    required = {"timestamp", "open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{name} missing columns: {sorted(missing)}")
    result = frame.copy()
    # Normalize the physical datetime resolution as well as the timezone.
    # Pandas 3 preserves second-vs-microsecond input resolution and merge_asof
    # refuses otherwise equivalent UTC keys with different dtypes.
    result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    for column in ("open", "high", "low", "close"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    if result[list(required)].isna().any().any():
        raise ValueError(f"{name} contains invalid candle values")
    if result["timestamp"].duplicated().any():
        raise ValueError(f"{name} contains duplicate timestamps")
    return result.sort_values("timestamp").reset_index(drop=True)


def _mean_or_none(values: list[float]) -> float | None:
    return float(np.mean(values)) if values else None


def _profit_factor(values: list[float]) -> float | None:
    if not values:
        return None
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = -sum(value for value in values if value < 0)
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else None
    return gross_profit / gross_loss


def _read_candles(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--one-hour", type=Path, required=True)
    parser.add_argument("--four-hour", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    payload = build_scanner_payload(
        _read_candles(args.one_hour),
        _read_candles(args.four_hour),
        symbol=args.symbol,
    )
    publish_scanner_payload(payload, args.out)
    print(
        f"{SCANNER_ID}: {payload['summary']['alerts']} research alerts; "
        "can_trade=false can_promote=false"
    )


if __name__ == "__main__":
    main()
