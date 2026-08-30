"""Regime-permitted HTF continuation on the next closed 15m decision.

This is a new research ID.  It reuses the frozen V1 pullback geometry but
does not use the quote-acceptance engine: the completed 15m reclaim is the
trigger and the runtime/backtester enters on the next-open clock.  Weekly,
daily, and 4h state can only deny a side.
"""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Final, Literal

import pandas as pd

from vnedge.data.candles import Candle
from vnedge.strategy.base_strategy import SignalIntent, StrategyExitIntent
from vnedge.strategy.market_regime import MarketRegime, MarketRegimeMachine
from vnedge.strategy.realtime_entry import RealtimeEntryArm
from vnedge.strategy.realtime_scanners import HtfStructureContinuationRealtimeV1

STRATEGY_ID: Final = "htf_regime_continuation_15m_v1"
STRATEGY_SPEC = MappingProxyType(
    {
        "strategy_id": STRATEGY_ID,
        "eligibility": "RESEARCH_ONLY",
        "capital_eligible": False,
        "tradeable": False,
        "timeframe": "15m",
        "context_timeframes": ("4h", "1d"),
        "entry_clock": "next_15m_open",
        "context": "closed weekly/daily/4h permission plus closed 15m reclaim",
    }
)


def _context_frame(candles: pd.DataFrame) -> pd.DataFrame:
    if candles.empty:
        return candles.copy()
    frame = candles.copy()
    if "timestamp" not in frame.columns and "open_time" in frame.columns:
        frame = frame.rename(columns={"open_time": "timestamp"})
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    return frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(
        drop=True
    )


def _append_candle(frame: pd.DataFrame, candle: Candle) -> pd.DataFrame:
    row = pd.DataFrame(
        [
            {
                "timestamp": candle.open_time,
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "volume": float(candle.volume),
                "quote_volume": float(candle.quote_volume),
                "trade_count": candle.trade_count,
                "vwap": float(candle.vwap) if candle.vwap is not None else math.nan,
                "is_closed": True,
                "data_quality": "ok",
            }
        ]
    )
    return _context_frame(pd.concat([frame, row], ignore_index=True)).tail(800).reset_index(drop=True)


class HtfRegimeContinuation15mV1(HtfStructureContinuationRealtimeV1):
    """Closed-bar successor to the quote-held HTF continuation scanner."""

    strategy_id = STRATEGY_ID
    eligibility = "RESEARCH_ONLY"
    canonical_context_timeframes = ("4h", "1d")

    def __init__(self, funding: pd.DataFrame | None = None) -> None:
        super().__init__(funding)
        self._regime_frames: dict[str, pd.DataFrame] = {
            "4h": pd.DataFrame(),
            "1d": pd.DataFrame(),
        }
        self._regime_health = {"4h": False, "1d": False}

    def bind_canonical_context(self, timeframe: str, candles: pd.DataFrame) -> None:
        if timeframe not in self.canonical_context_timeframes:
            raise ValueError(f"unsupported market-regime context timeframe: {timeframe}")
        frame = _context_frame(candles)
        self._regime_frames[timeframe] = frame
        self._regime_health[timeframe] = not frame.empty
        if timeframe == "4h":
            super().bind_canonical_context(timeframe, frame)

    def ingest_canonical_context(self, candle: Candle) -> None:
        if candle.timeframe not in self.canonical_context_timeframes:
            raise ValueError(
                f"unsupported market-regime context timeframe: {candle.timeframe}"
            )
        self._regime_frames[candle.timeframe] = _append_candle(
            self._regime_frames[candle.timeframe], candle
        )
        self._regime_health[candle.timeframe] = True
        if candle.timeframe == "4h":
            super().ingest_canonical_context(candle)

    def set_canonical_context_health(self, timeframe: str, healthy: bool) -> None:
        if timeframe not in self.canonical_context_timeframes:
            raise ValueError(f"unsupported market-regime context timeframe: {timeframe}")
        self._regime_health[timeframe] = bool(healthy)
        if timeframe == "4h":
            super().set_canonical_context_health(timeframe, healthy)

    def _regime_at(
        self,
        decision_at: pd.Timestamp,
        *,
        machine: MarketRegimeMachine | None = None,
    ) -> MarketRegime:
        regime_machine = machine or MarketRegimeMachine()
        return regime_machine.step(
            self._regime_frames["1d"],
            self._regime_frames["4h"],
            as_of=decision_at,
            data_healthy=all(self._regime_health.values()),
            health_reason="canonical_regime_context_unhealthy",
        )

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out = super().prepare(candles)
        regime_machine = MarketRegimeMachine()
        regimes = [
            self._regime_at(
                pd.Timestamp(ts) + pd.Timedelta(minutes=15),
                machine=regime_machine,
            )
            for ts in pd.to_datetime(out["timestamp"], utc=True)
        ]
        out["mreg_weekly"] = [item.weekly for item in regimes]
        out["mreg_daily"] = [item.daily for item in regimes]
        out["mreg_h4"] = [item.h4 for item in regimes]
        out["mreg_ema_state"] = [item.ema_state for item in regimes]
        out["mreg_macd_impulse"] = [item.macd_impulse for item in regimes]
        out["mreg_rsi_zone"] = [item.rsi_zone for item in regimes]
        out["mreg_daily_ema21"] = [item.daily_ema21 for item in regimes]
        out["mreg_daily_ema50"] = [item.daily_ema50 for item in regimes]
        out["mreg_daily_ema200"] = [item.daily_ema200 for item in regimes]
        out["mreg_daily_macd_hist"] = [item.daily_macd_hist for item in regimes]
        out["mreg_h4_macd_hist"] = [item.h4_macd_hist for item in regimes]
        out["mreg_daily_rsi"] = [item.daily_rsi for item in regimes]
        out["mreg_state"] = [item.state for item in regimes]
        out["mreg_family"] = out["mreg_state"]  # schema-compatibility alias
        out["mreg_reason"] = [item.reason for item in regimes]
        out["mreg_exit_reason"] = [item.exit_reason for item in regimes]
        out["mreg_asof_tf"] = [
            item.asof_bar[0] if item.asof_bar is not None else None for item in regimes
        ]
        out["mreg_asof_close_time"] = [
            item.asof_bar[1] if item.asof_bar is not None else None for item in regimes
        ]
        out["mreg_ready"] = [float(item.ready) for item in regimes]
        long_permission = pd.Series(
            [item.allows_scanner("htf", "long") for item in regimes],
            index=out.index,
        )
        short_permission = pd.Series(
            [item.allows_scanner("htf", "short") for item in regimes],
            index=out.index,
        )
        out["mreg_allow_long"] = long_permission.astype(float)
        out["mreg_allow_short"] = short_permission.astype(float)
        out["rt_allow_long"] = (
            out["rt_allow_long"].eq(1) & long_permission
        ).astype(float)
        out["rt_allow_short"] = (
            out["rt_allow_short"].eq(1) & short_permission
        ).astype(float)
        out["rt_arm_ready"] = (
            out["rt_allow_long"].eq(1) | out["rt_allow_short"].eq(1)
        ).astype(float)
        return out

    def realtime_arm(self, df: pd.DataFrame, index: int) -> RealtimeEntryArm | None:
        del df, index
        return None

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index < self.warmup_bars or index >= len(df):
            return None
        row = df.iloc[index]
        long_ready = bool(float(row.get("rt_allow_long", 0)))
        short_ready = bool(float(row.get("rt_allow_short", 0)))
        if long_ready == short_ready:
            return None
        side: Literal["long", "short"] = "long" if long_ready else "short"
        stop_key = "rt_long_structural_stop" if side == "long" else "rt_short_structural_stop"
        stop = float(row.get(stop_key, math.nan))
        close = float(row.get("close", math.nan))
        if not all(math.isfinite(value) and value > 0 for value in (stop, close)):
            return None
        if (side == "long" and stop >= close) or (side == "short" and stop <= close):
            return None
        return SignalIntent(
            side=side,
            stop_price=stop,
            take_profit_price=None,
            reason=(
                f"{self.strategy_id} {side} closed_15m_reclaim "
                f"weekly={row['mreg_weekly']} daily={row['mreg_daily']} "
                f"h4={row['mreg_h4']} ema={row['mreg_ema_state']} "
                f"macd={row['mreg_macd_impulse']} rsi={row['mreg_rsi_zone']}"
            ),
        )

    def exit_signal(
        self,
        df: pd.DataFrame,
        index: int,
        side: str,
        entry_price: float,
    ) -> StrategyExitIntent | None:
        del entry_price
        if index < 0 or index >= len(df) or side not in {"long", "short"}:
            return None
        row = df.iloc[index]
        state = str(row.get("mreg_state", "flat"))
        structurally_aligned = (
            side == "long"
            and str(row.get("mreg_weekly")) != "down"
            and str(row.get("mreg_ema_state")) == "up"
            and str(row.get("mreg_h4")) == "up"
        ) or (
            side == "short"
            and str(row.get("mreg_weekly")) != "up"
            and str(row.get("mreg_ema_state")) == "down"
            and str(row.get("mreg_h4")) == "down"
        )
        if state == "continuation" and structurally_aligned:
            return None
        close = float(row.get("close", math.nan))
        return StrategyExitIntent(
            reason="htf_bias_invalidated",
            exit_price=close if math.isfinite(close) and close > 0 else None,
        )

    def evaluation_diagnostics(self, df: pd.DataFrame, index: int) -> dict[str, object]:
        diagnostics = super().evaluation_diagnostics(df, index)
        row = df.iloc[index]
        features = dict(diagnostics.get("features", {}))
        features.update(
            {
                "regime_weekly": str(row.get("mreg_weekly", "range")),
                "regime_daily": str(row.get("mreg_daily", "mid")),
                "regime_h4": str(row.get("mreg_h4", "range")),
                "regime_ema_state": str(row.get("mreg_ema_state", "range")),
                "regime_macd_impulse": str(row.get("mreg_macd_impulse", "off")),
                "regime_rsi_zone": str(row.get("mreg_rsi_zone", "mid")),
                "daily_ema21": row.get("mreg_daily_ema21"),
                "daily_ema50": row.get("mreg_daily_ema50"),
                "daily_ema200": row.get("mreg_daily_ema200"),
                "daily_macd_hist": row.get("mreg_daily_macd_hist"),
                "h4_macd_hist": row.get("mreg_h4_macd_hist"),
                "daily_rsi": row.get("mreg_daily_rsi"),
                "regime_state": str(row.get("mreg_state", "flat")),
                "regime_family": str(row.get("mreg_state", "flat")),
                "regime_reason": str(row.get("mreg_reason", "unavailable")),
                "regime_exit_reason": row.get("mreg_exit_reason"),
                "regime_asof_tf": row.get("mreg_asof_tf"),
                "regime_asof_close_time": row.get("mreg_asof_close_time"),
                "entry_mode": "next_open_after_closed_15m_reclaim",
            }
        )
        failures = list(diagnostics.get("all_failed_gates", []))
        if not bool(float(row.get("mreg_ready", 0))):
            failures.insert(0, "market_regime_not_ready")
        elif str(row.get("mreg_state")) != "continuation":
            failures.insert(0, "market_regime_playbook_blocked")
        diagnostics["features"] = features
        diagnostics["all_failed_gates"] = failures
        diagnostics["primary_failed_gate"] = failures[0] if failures else None
        diagnostics["eligible"] = bool(float(row.get("rt_arm_ready", 0)))
        return diagnostics


__all__ = ["STRATEGY_ID", "STRATEGY_SPEC", "HtfRegimeContinuation15mV1"]
