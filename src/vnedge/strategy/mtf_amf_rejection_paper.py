"""Paper-only MTF/AMF rejection strategy.

This wraps the research-only ``mtf_amf_rejection_scanner_v1`` in the normal
VNEDGE strategy contract so it can be live-forward tested in PAPER mode.  It is
not a live-capital promotion: the class is explicitly marked ``paper_only`` and
the live-trader entrypoint refuses paper-only strategies before any exchange
client is constructed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from vnedge.research.mtf_amf_rejection_scanner import (
    DEFAULT_CONFIG,
    MtfAmfScannerConfig,
    build_mtf_amf_feature_frame,
)
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent, StrategyExitIntent

MTF_AMF_REJECTION_PAPER_ID = "mtf_amf_rejection_paper_v1"
_SIDES = ("long", "short")


class MtfAmfRejectionPaperStrategy(BaseStrategy):
    """Completed-HTF level rejection with AMF alignment, for PAPER only."""

    strategy_id = MTF_AMF_REJECTION_PAPER_ID
    paper_only = True

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        scanner_config: MtfAmfScannerConfig | None = None,
        stop_atr_buffer: float = 0.15,
        min_stop_bps: float = 25.0,
        tp_r_multiples: tuple[float, ...] | list[float] = (1.0, 1.8, 2.8),
        exit_on_opposite: bool = True,
        exit_on_regime_escape: bool = True,
        ranging_regime_exit: float = 0.70,
    ) -> None:
        if stop_atr_buffer < 0:
            raise ValueError("stop_atr_buffer must be non-negative")
        if min_stop_bps <= 0:
            raise ValueError("min_stop_bps must be positive")
        tp_levels = tuple(float(value) for value in tp_r_multiples)
        if not tp_levels or any(value <= 0 for value in tp_levels):
            raise ValueError("tp_r_multiples must contain positive values")
        if tuple(sorted(tp_levels)) != tp_levels:
            raise ValueError("tp_r_multiples must be ascending")
        if not 0 < ranging_regime_exit <= 1:
            raise ValueError("ranging_regime_exit must be in (0, 1]")

        self.funding = funding
        self.config = scanner_config or DEFAULT_CONFIG
        self.stop_atr_buffer = stop_atr_buffer
        self.min_stop_bps = min_stop_bps
        self.tp_r_multiples = tp_levels
        self.exit_on_opposite = exit_on_opposite
        self.exit_on_regime_escape = exit_on_regime_escape
        self.ranging_regime_exit = max(ranging_regime_exit, self.config.ranging_regime_max)
        self.warmup_bars = self.config.warmup_bars

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        one_hour = _canonical_one_hour(candles)
        if one_hour.empty:
            return _with_signal_columns(one_hour)
        four_hour = _completed_four_hour(one_hour)
        frame = build_mtf_amf_feature_frame(one_hour, four_hour, config=self.config)
        return _mark_cooldown_signals(frame, self.config)

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index < self.warmup_bars or index >= len(df):
            return None
        row = df.iloc[index]
        side = str(row.get("mtf_amf_signal_side") or "")
        if side not in _SIDES:
            return None
        return self._intent_from_row(row, side)

    def exit_signal(
        self,
        df: pd.DataFrame,
        index: int,
        side: str,
        entry_price: float,
    ) -> StrategyExitIntent | None:
        if side not in _SIDES or index < self.warmup_bars or index >= len(df):
            return None
        row = df.iloc[index]
        if _has_nan(row, ("close", "amf_regime")):
            return None
        close = float(row["close"])
        raw_side = str(row.get("mtf_amf_raw_side") or "")
        if self.exit_on_opposite and raw_side in _SIDES and raw_side != side:
            return StrategyExitIntent(
                reason=f"mtf_amf_opposite_rejection_exit_{side}",
                exit_price=close,
            )
        if self.exit_on_regime_escape and float(row["amf_regime"]) >= self.ranging_regime_exit:
            return StrategyExitIntent(
                reason=(
                    f"mtf_amf_regime_escape_exit_{side}: "
                    f"regime={float(row['amf_regime']):.2f}"
                ),
                exit_price=close,
            )
        return None

    def synthesize_exit_plan(
        self,
        df: pd.DataFrame,
        index: int,
        side: str,
        entry_price: float,
    ) -> SignalIntent | None:
        if side not in _SIDES or index >= len(df):
            return None
        row = df.iloc[index]
        if _has_nan(row, ("atr", "mtf_amf_level")):
            return None
        return self._intent_from_row(row, side, reference_price=float(entry_price))

    def _intent_from_row(
        self,
        row: pd.Series,
        side: str,
        *,
        reference_price: float | None = None,
    ) -> SignalIntent | None:
        if _has_nan(row, ("close", "atr", "mtf_amf_level")):
            return None
        close = float(row["close"] if reference_price is None else reference_price)
        atr_value = float(row["atr"])
        level = float(row["mtf_amf_level"])
        if close <= 0 or atr_value <= 0:
            return None

        min_stop_distance = close * self.min_stop_bps / 10_000.0
        if side == "long":
            structural = min(_safe_float(row.get("low"), close), level)
            stop = min(structural - self.stop_atr_buffer * atr_value, close - min_stop_distance)
            distance = close - stop
            if stop <= 0 or distance <= 0:
                return None
            levels = tuple(close + multiple * distance for multiple in self.tp_r_multiples)
        else:
            structural = max(_safe_float(row.get("high"), close), level)
            stop = max(structural + self.stop_atr_buffer * atr_value, close + min_stop_distance)
            distance = stop - close
            if distance <= 0:
                return None
            levels = tuple(close - multiple * distance for multiple in self.tp_r_multiples)
            if any(level_value <= 0 for level_value in levels):
                return None

        return SignalIntent(
            side=side,
            stop_price=stop,
            take_profit_price=levels[-1],
            take_profit_levels=levels,
            reason=(
                f"PAPER_ONLY mtf_amf_rejection {side}; "
                "tf=1h_trigger/completed_4h_context; "
                f"level={level:.6g}; distanceATR="
                f"{_safe_float(row.get('mtf_amf_distance_atr'), 0.0):.2f}; "
                f"amfHist={_safe_float(row.get('amf_histogram'), 0.0):+.6g}; "
                f"amfRegime={_safe_float(row.get('amf_regime'), 0.0):.2f}; "
                f"tp_ladder={'/'.join(f'{value:.6g}' for value in levels)}; "
                "exit=TP1_partial_BE_TP2_trail_max62"
            ),
        )


def _canonical_one_hour(candles: pd.DataFrame) -> pd.DataFrame:
    df = candles.copy()
    if "timestamp" not in df.columns:
        raise ValueError("candles missing timestamp")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    for column in ("open", "high", "low", "close"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    if df[["timestamp", "open", "high", "low", "close"]].isna().any().any():
        raise ValueError("candles contain invalid OHLC values")
    return df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def _completed_four_hour(one_hour: pd.DataFrame) -> pd.DataFrame:
    if one_hour.empty:
        return pd.DataFrame(
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
    indexed = one_hour.set_index("timestamp")
    four = (
        indexed.resample("4h", label="left", closed="left")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )
    return four


def _mark_cooldown_signals(
    frame: pd.DataFrame,
    config: MtfAmfScannerConfig,
) -> pd.DataFrame:
    df = _with_signal_columns(frame.copy())
    blocked_until = -1
    for pos, row in df.iterrows():
        raw = _raw_side(row, config)
        if raw is None:
            continue
        if pos >= config.warmup_bars and pos >= blocked_until:
            df.at[pos, "mtf_amf_signal_side"] = raw.side
            df.at[pos, "mtf_amf_reason"] = raw.reason
            blocked_until = pos + config.cooldown_bars + 1
        df.at[pos, "mtf_amf_raw_side"] = raw.side
        df.at[pos, "mtf_amf_level"] = raw.level
        df.at[pos, "mtf_amf_distance_atr"] = raw.distance_atr
    return df


def _with_signal_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["mtf_amf_signal_side"] = ""
    out["mtf_amf_raw_side"] = ""
    out["mtf_amf_level"] = float("nan")
    out["mtf_amf_distance_atr"] = float("nan")
    out["mtf_amf_reason"] = ""
    return out


@dataclass(frozen=True)
class _RawSignal:
    side: str
    level: float
    distance_atr: float
    reason: str


def _raw_side(row: pd.Series, config: MtfAmfScannerConfig) -> _RawSignal | None:
    required = (
        "atr",
        "amf_histogram",
        "amf_regime",
        "one_hour_high",
        "one_hour_low",
        "four_hour_high",
        "four_hour_low",
        "upper_distance_atr",
        "lower_distance_atr",
        "upper_level",
        "lower_level",
    )
    if _has_nan(row, required):
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
        return _RawSignal(
            side="short",
            level=float(row["upper_level"]),
            distance_atr=float(row["upper_distance_atr"]),
            reason="completed_mtf_upper_level_rejection_with_amf_bearish",
        )
    if long_rejection:
        return _RawSignal(
            side="long",
            level=float(row["lower_level"]),
            distance_atr=float(row["lower_distance_atr"]),
            reason="completed_mtf_lower_level_rejection_with_amf_bullish",
        )
    return None


def _has_nan(row: pd.Series, columns: tuple[str, ...]) -> bool:
    for column in columns:
        value = row.get(column)
        if value is None or pd.isna(value):
            return True
    return False


def _safe_float(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number):
        return default
    return number
