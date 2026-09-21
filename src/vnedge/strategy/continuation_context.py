"""Bounded higher-timeframe evidence and regime state for continuation scanners.

No fetching, setup geometry, orders, or permissions to execute live. The scanner
still forwards 4h evidence to its independent structure engine.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import pandas as pd

from vnedge.data.bar_identity import bar_content_sha256
from vnedge.data.candles import Candle
from vnedge.strategy.market_regime import MarketRegime, MarketRegimeMachine


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
    values: dict[str, object] = {
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
        "coverage_ok": True,
        "timeframe": candle.timeframe,
        "symbol": candle.symbol,
        "candle_source": "canonical_tick_lake",
    }
    values["content_sha256"] = bar_content_sha256(
        values,
        open_time=candle.open_time,
        close_time=candle.close_time,
        source="canonical_tick_lake",
    )
    row = pd.DataFrame([values])
    return _context_frame(pd.concat([frame, row], ignore_index=True)).tail(800).reset_index(drop=True)


class ContinuationContext:
    """Own bound HTF frames, health, weekly artifacts and the live regime fold."""

    canonical_context_timeframes = ("4h", "1d")

    def __init__(self, machine_factory: Callable[[], MarketRegimeMachine]) -> None:
        self._new_regime_machine = machine_factory
        self.frames = {"4h": pd.DataFrame(), "1d": pd.DataFrame()}
        self.health = {"4h": False, "1d": False}
        self.weekly_vwap_artifacts = pd.DataFrame()
        self.weekly_vwap_symbol: str | None = None
        self.live_machine = machine_factory()

    def identity(self, decision_at: pd.Timestamp) -> tuple[object, ...]:
        """Return the last closed HTF identities available to a decision.

        Fifteen-minute rows between two HTF closes share one immutable regime
        snapshot.  This avoids recomputing the daily/weekly telescope on every
        microscope bar without changing V1's classifier semantics.
        """
        identities: list[object] = [all(self.health.values())]
        for timeframe, delta in (
            ("4h", pd.Timedelta(hours=4)),
            ("1d", pd.Timedelta(days=1)),
        ):
            frame = self.frames[timeframe]
            if frame.empty:
                identities.append(None)
                continue
            timestamps = pd.to_datetime(frame["timestamp"], utc=True)
            eligible = timestamps[timestamps + delta <= decision_at]
            identities.append(int(eligible.iloc[-1].value) if not eligible.empty else None)
        if self.weekly_vwap_artifacts.empty:
            identities.append(None)
        else:
            close_times = pd.to_datetime(
                self.weekly_vwap_artifacts["close_time"], utc=True, errors="coerce"
            )
            eligible = close_times[close_times <= decision_at]
            identities.append(int(eligible.iloc[-1].value) if not eligible.empty else None)
        return tuple(identities)

    def bind_weekly_vwap_artifacts(self, artifacts: pd.DataFrame) -> None:
        """Bind the cached Delta trade-lake weekly VWAP dataset.

        This is intentionally separate from ``bind_canonical_context``:
        official OHLC candles cannot satisfy the trade-lake VWAP contract.
        The regime loop only reads this bounded cache and never scans trades.
        """
        frame = artifacts.copy()
        if not frame.empty:
            required = {
                "exchange",
                "symbol",
                "timeframe",
                "open_time",
                "close_time",
                "vwap",
                "sum_base",
                "sum_notional",
                "n_trades",
                "source",
                "coverage_ok",
            }
            missing = required.difference(frame.columns)
            if missing:
                raise ValueError(f"weekly VWAP artifacts missing columns: {sorted(missing)}")
            frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True, errors="coerce")
            frame["close_time"] = pd.to_datetime(
                frame["close_time"], utc=True, errors="coerce"
            )
            frame = (
                frame.sort_values("open_time")
                .drop_duplicates("open_time", keep="last")
                .tail(64)
                .reset_index(drop=True)
            )
            symbols = set(frame["symbol"].astype(str))
            if len(symbols) != 1:
                raise ValueError("weekly VWAP cache must contain exactly one symbol")
            self.weekly_vwap_symbol = symbols.pop()
        else:
            self.weekly_vwap_symbol = None
        self.weekly_vwap_artifacts = frame

    def bind_canonical_context(self, timeframe: str, candles: pd.DataFrame) -> None:
        if timeframe not in self.canonical_context_timeframes:
            raise ValueError(f"unsupported market-regime context timeframe: {timeframe}")
        frame = _context_frame(candles)
        self.frames[timeframe] = frame
        self.health[timeframe] = not frame.empty

    def ingest_canonical_context(self, candle: Candle) -> None:
        if candle.timeframe not in self.canonical_context_timeframes:
            raise ValueError(
                f"unsupported market-regime context timeframe: {candle.timeframe}"
            )
        self.frames[candle.timeframe] = _append_candle(
            self.frames[candle.timeframe], candle
        )
        self.health[candle.timeframe] = True

    def set_canonical_context_health(self, timeframe: str, healthy: bool) -> None:
        if timeframe not in self.canonical_context_timeframes:
            raise ValueError(f"unsupported market-regime context timeframe: {timeframe}")
        self.health[timeframe] = bool(healthy)

    def regime_at(
        self,
        decision_at: pd.Timestamp,
        *,
        machine: MarketRegimeMachine | None = None,
    ) -> MarketRegime:
        regime_machine = machine or self._new_regime_machine()
        return regime_machine.step(
            self.frames["1d"],
            self.frames["4h"],
            as_of=decision_at,
            weekly_vwap_artifacts=self.weekly_vwap_artifacts,
            weekly_vwap_symbol=self.weekly_vwap_symbol,
            data_healthy=all(self.health.values()),
            health_reason="canonical_regime_context_unhealthy",
        )


def annotate_regimes(
    out: pd.DataFrame, regimes: list[MarketRegime], *, weekly_classifier: str,
) -> None:
    """Write display fields only; admission flags remain the scanner's job."""
    out["mreg_weekly"] = [item.weekly for item in regimes]
    out["mreg_weekly_classifier"] = weekly_classifier
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
    out["mreg_daily_observations"] = [item.daily_observations for item in regimes]
    out["mreg_ema200_ready"] = [float(item.ema200_ready) for item in regimes]
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
