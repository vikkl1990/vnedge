"""Five causal, pre-registered scanner mechanisms (research/shadow only).

The implementations intentionally share only data-quality and stop/target
helpers.  Each mechanism has an independent market claim and strategy id.
They emit virtual ``SignalIntent`` objects through the existing scanner path;
the registry keeps every id non-capital.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Literal

import pandas as pd  # type: ignore[import-untyped]

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr, ema, prior_high, prior_low
from vnedge.strategy.regime_router import annotate_strategy_route


def _frame(candles: pd.DataFrame, *, name: str) -> tuple[pd.DataFrame, pd.Series]:
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required.difference(candles.columns)
    if missing:
        raise ValueError(f"{name} missing columns: {sorted(missing)}")
    out = candles.copy()
    ts = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    if ts.isna().any():
        raise ValueError(f"{name} requires valid UTC timestamps")
    for name_ in ("open", "high", "low", "close", "volume"):
        out[name_] = pd.to_numeric(out[name_], errors="coerce")
    quality = (
        out["data_quality"].astype(str).str.lower().eq("ok")
        if "data_quality" in out.columns else pd.Series(True, index=out.index)
    )
    closed = (
        out["is_closed"].astype(bool)
        if "is_closed" in out.columns else pd.Series(True, index=out.index)
    )
    out["rs_quality_ok"] = (quality & closed).astype(float)
    out = annotate_strategy_route(out, name)
    out["rs_route_ok"] = out["regime_route_allowed"].astype(float)
    return out, ts


def _intent(side: Literal["long", "short"], close: float, risk: float,
            *, reward_r: float, reason: str) -> SignalIntent | None:
    if not all(math.isfinite(value) and value > 0 for value in (close, risk)):
        return None
    stop = close - risk if side == "long" else close + risk
    target = close + reward_r * risk if side == "long" else close - reward_r * risk
    if min(stop, target) <= 0:
        return None
    return SignalIntent(side=side, stop_price=stop, take_profit_price=target, reason=reason)


class _DiagnosticScanner(BaseStrategy):
    prefix = "rs"
    failure_contract: tuple[tuple[str, str], ...] = ()
    threshold_contract: ClassVar[dict[str, float]] = {}

    def __init__(self, funding: pd.DataFrame | None = None) -> None:
        # Kept for the common registry/causality factory contract.  These five
        # mechanisms are price/volume-only and never inspect funding.
        self.funding = funding

    def evaluation_diagnostics(self, df: pd.DataFrame, index: int) -> dict[str, Any]:
        row = df.iloc[index]
        features: dict[str, Any] = {}
        failures: list[str] = []
        for column, reason in self.failure_contract:
            value = row.get(column)
            try:
                numeric = float(value)
                ready = math.isfinite(numeric) and numeric > 0
                features[column] = numeric if math.isfinite(numeric) else None
            except (TypeError, ValueError):
                ready = bool(value)
                features[column] = value
            if not ready:
                failures.append(reason)
        for column in df.columns:
            if column.startswith(f"{self.prefix}_") and column not in features:
                value = row.get(column)
                if isinstance(value, (int, float)):
                    features[column] = float(value) if math.isfinite(float(value)) else None
                elif isinstance(value, (str, bool)):
                    features[column] = value
        eligible = not failures and bool(features.get(f"{self.prefix}_fire", 0))
        return {
            "eligible": eligible,
            "primary_failed_gate": failures[0] if failures else None,
            "all_failed_gates": failures,
            "features": features,
            "thresholds": dict(self.threshold_contract),
            "distance_to_threshold": {},
        }


@dataclass(frozen=True, slots=True)
class AvwapReclaimParams:
    swing_left: int = 3
    swing_right: int = 3
    atr_period: int = 20
    min_excursion_bps: float = 15.0
    reward_r: float = 2.0


class AvwapReclaim15mV1(_DiagnosticScanner):
    strategy_id = "avwap_reclaim_15m_v1"
    eligibility = "RESEARCH_ONLY"
    timeframe = "15m"
    params = AvwapReclaimParams()
    warmup_bars = 21
    prefix = "avr"
    failure_contract = (
        ("rs_quality_ok", "data_quality_not_ok"),
        ("rs_route_ok", "regime_route_blocked"),
        ("avr_exact_volume", "exact_volume_window_not_ready"),
        ("avr_excursion_ok", "avwap_excursion_too_small"),
        ("avr_reclaim", "avwap_not_reclaimed"),
        ("avr_fire", "no_reclaim_setup"),
    )
    threshold_contract: ClassVar[dict[str, float]] = {
        "min_excursion_bps": 15.0, "swing_left": 3.0, "swing_right": 3.0
    }

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out, _ = _frame(candles, name=self.strategy_id)
        p = self.params
        volume = out["volume"]
        quote = (
            pd.to_numeric(out["quote_volume"], errors="coerce")
            if "quote_volume" in out.columns
            else pd.Series(float("nan"), index=out.index)
        )
        # A pivot at i becomes usable only at i+right. When confirmed, the
        # AVWAP includes exact quote/base volume from the original event bar;
        # no child VWAP averaging and no HLC proxy are permitted.
        base_cum = volume.fillna(0).cumsum()
        quote_cum = quote.fillna(0).cumsum()
        avwap_values = [float("nan")] * len(out)
        anchor_indices = [-1] * len(out)
        anchor_kinds = ["none"] * len(out)
        active_anchor = -1
        active_kind = "none"
        left, right = p.swing_left, p.swing_right
        for decision in range(len(out)):
            candidate = decision - right
            if candidate >= left:
                start, stop = candidate - left, candidate + right + 1
                lows = out["low"].iloc[start:stop]
                highs = out["high"].iloc[start:stop]
                if len(lows) == left + right + 1:
                    pivot_low = float(out["low"].iloc[candidate])
                    pivot_high = float(out["high"].iloc[candidate])
                    low_confirmed = pivot_low < float(lows.drop(index=out.index[candidate]).min())
                    high_confirmed = pivot_high > float(highs.drop(index=out.index[candidate]).max())
                    if low_confirmed or high_confirmed:
                        active_anchor = candidate
                        active_kind = "low" if low_confirmed else "high"
            if active_anchor < 0:
                continue
            prior_base = float(base_cum.iloc[active_anchor - 1]) if active_anchor else 0.0
            prior_quote = float(quote_cum.iloc[active_anchor - 1]) if active_anchor else 0.0
            base_sum = float(base_cum.iloc[decision]) - prior_base
            quote_sum = float(quote_cum.iloc[decision]) - prior_quote
            if base_sum > 0 and quote_sum > 0 and quote.iloc[active_anchor:decision + 1].notna().all():
                avwap_values[decision] = quote_sum / base_sum
                anchor_indices[decision] = active_anchor
                anchor_kinds[decision] = active_kind
        avwap = pd.Series(avwap_values, index=out.index)
        distance = (out["close"] - avwap).div(avwap).mul(10_000)
        prior_distance = distance.shift(1)
        reclaim_long = prior_distance.le(-p.min_excursion_bps) & distance.gt(0)
        reclaim_short = prior_distance.ge(p.min_excursion_bps) & distance.lt(0)
        exact = avwap.notna()
        fire = exact & out["rs_quality_ok"].eq(1) & out["rs_route_ok"].eq(1) & (reclaim_long | reclaim_short)
        out["avr_avwap"] = avwap
        out["avr_anchor_index"] = anchor_indices
        out["avr_anchor_kind"] = anchor_kinds
        out["avr_distance_bps"] = distance
        out["avr_exact_volume"] = exact.astype(float)
        out["avr_excursion_ok"] = prior_distance.abs().ge(p.min_excursion_bps).astype(float)
        out["avr_reclaim"] = (reclaim_long | reclaim_short).astype(float)
        out["avr_side"] = reclaim_long.map({True: "long", False: "short"})
        out["avr_atr"] = atr(out, p.atr_period)
        out["avr_fire"] = fire.astype(float)
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index < self.warmup_bars or not bool(df.iloc[index]["avr_fire"]):
            return None
        row = df.iloc[index]
        side: Literal["long", "short"] = str(row["avr_side"])  # type: ignore[assignment]
        return _intent(side, float(row["close"]), float(row["avr_atr"]),
                       reward_r=self.params.reward_r,
                       reason=f"avwap_reclaim_15m_v1 {side} closed_bar virtual_only")


@dataclass(frozen=True, slots=True)
class SessionContinuationParams:
    range_bars: int = 4
    volume_bars: int = 32
    atr_period: int = 20
    start_hour: int = 12
    end_hour: int = 16
    reward_r: float = 2.0


class SessionContinuation15mV1(_DiagnosticScanner):
    strategy_id = "session_continuation_15m_v1"
    eligibility = "RESEARCH_ONLY"
    timeframe = "15m"
    params = SessionContinuationParams()
    warmup_bars = 33
    prefix = "sc15"
    failure_contract = (
        ("rs_quality_ok", "data_quality_not_ok"),
        ("rs_route_ok", "regime_route_blocked"),
        ("sc15_session_ok", "session_closed"),
        ("sc15_volume_ok", "volume_confirmation_failed"),
        ("sc15_break", "continuation_level_not_broken"),
        ("sc15_fire", "no_continuation_setup"),
    )
    threshold_contract: ClassVar[dict[str, float]] = {
        "session_start_hour": 12.0, "session_end_hour": 16.0
    }

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out, ts = _frame(candles, name=self.strategy_id)
        p = self.params
        high = prior_high(out["high"], p.range_bars)
        low = prior_low(out["low"], p.range_bars)
        vol_base = out["volume"].shift(1).rolling(p.volume_bars).median()
        session = ts.dt.hour.ge(p.start_hour) & ts.dt.hour.lt(p.end_hour)
        volume_ok = out["volume"].ge(vol_base)
        long = out["close"].gt(high) & out["close"].gt(out["open"])
        short = out["close"].lt(low) & out["close"].lt(out["open"])
        fire = session & volume_ok & out["rs_quality_ok"].eq(1) & out["rs_route_ok"].eq(1) & (long | short)
        out["sc15_session_ok"] = session.astype(float)
        out["sc15_volume_ok"] = volume_ok.astype(float)
        out["sc15_break"] = (long | short).astype(float)
        out["sc15_side"] = long.map({True: "long", False: "short"})
        out["sc15_atr"] = atr(out, p.atr_period)
        out["sc15_fire"] = fire.astype(float)
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index < self.warmup_bars or not bool(df.iloc[index]["sc15_fire"]):
            return None
        row = df.iloc[index]
        side: Literal["long", "short"] = str(row["sc15_side"])  # type: ignore[assignment]
        return _intent(side, float(row["close"]), float(row["sc15_atr"]),
                       reward_r=self.params.reward_r,
                       reason=f"session_continuation_15m_v1 {side} 12-16UTC virtual_only")


@dataclass(frozen=True, slots=True)
class LiquiditySweepParams:
    lookback: int = 20
    atr_period: int = 20
    min_wick_bps: float = 5.0
    reward_r: float = 1.8


class LiquiditySweepReversal15mV1(_DiagnosticScanner):
    strategy_id = "liquidity_sweep_reversal_15m_v1"
    eligibility = "RESEARCH_ONLY"
    timeframe = "15m"
    params = LiquiditySweepParams()
    warmup_bars = 21
    prefix = "lsr"
    failure_contract = (
        ("rs_quality_ok", "data_quality_not_ok"),
        ("rs_route_ok", "regime_route_blocked"),
        ("lsr_sweep", "liquidity_level_not_swept"),
        ("lsr_rejection", "sweep_not_rejected"),
        ("lsr_fire", "no_sweep_reversal_setup"),
    )
    threshold_contract: ClassVar[dict[str, float]] = {
        "lookback": 20.0, "min_wick_bps": 5.0
    }

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out, _ = _frame(candles, name=self.strategy_id)
        p = self.params
        ph = prior_high(out["high"], p.lookback)
        pl = prior_low(out["low"], p.lookback)
        long = out["low"].lt(pl) & out["close"].gt(pl)
        short = out["high"].gt(ph) & out["close"].lt(ph)
        wick_long = (pl - out["low"]).div(out["close"]).mul(10_000)
        wick_short = (out["high"] - ph).div(out["close"]).mul(10_000)
        wick_ok = (long & wick_long.ge(p.min_wick_bps)) | (short & wick_short.ge(p.min_wick_bps))
        fire = out["rs_quality_ok"].eq(1) & out["rs_route_ok"].eq(1) & wick_ok
        out["lsr_sweep"] = (out["low"].lt(pl) | out["high"].gt(ph)).astype(float)
        out["lsr_rejection"] = (long | short).astype(float)
        out["lsr_wick_bps"] = wick_long.where(long, wick_short)
        out["lsr_side"] = long.map({True: "long", False: "short"})
        out["lsr_atr"] = atr(out, p.atr_period)
        out["lsr_fire"] = fire.astype(float)
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index < self.warmup_bars or not bool(df.iloc[index]["lsr_fire"]):
            return None
        row = df.iloc[index]
        side: Literal["long", "short"] = str(row["lsr_side"])  # type: ignore[assignment]
        return _intent(side, float(row["close"]), float(row["lsr_atr"]),
                       reward_r=self.params.reward_r,
                       reason=f"liquidity_sweep_reversal_15m_v1 {side} virtual_only")


@dataclass(frozen=True, slots=True)
class TrendPullbackParams:
    fast_ema: int = 20
    slow_ema: int = 50
    atr_period: int = 20
    reward_r: float = 2.5


class TrendPullback1hV1(_DiagnosticScanner):
    strategy_id = "trend_pullback_1h_v1"
    eligibility = "RESEARCH_ONLY"
    timeframe = "1h"
    params = TrendPullbackParams()
    warmup_bars = 51
    prefix = "tp1h"
    failure_contract = (
        ("rs_quality_ok", "data_quality_not_ok"),
        ("rs_route_ok", "regime_route_blocked"),
        ("tp1h_trend_ok", "ema_trend_not_established"),
        ("tp1h_pullback", "pullback_not_present"),
        ("tp1h_resume", "trend_not_resumed"),
        ("tp1h_fire", "no_trend_pullback_setup"),
    )
    threshold_contract: ClassVar[dict[str, float]] = {
        "fast_ema": 20.0, "slow_ema": 50.0
    }

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out, _ = _frame(candles, name=self.strategy_id)
        p = self.params
        fast = ema(out["close"], p.fast_ema)
        slow = ema(out["close"], p.slow_ema)
        up = fast.gt(slow) & fast.gt(fast.shift(1))
        down = fast.lt(slow) & fast.lt(fast.shift(1))
        pull_long = out["low"].shift(1).le(fast.shift(1)) & out["close"].shift(1).ge(slow.shift(1))
        pull_short = out["high"].shift(1).ge(fast.shift(1)) & out["close"].shift(1).le(slow.shift(1))
        resume_long = out["close"].gt(out["high"].shift(1)) & out["close"].gt(fast)
        resume_short = out["close"].lt(out["low"].shift(1)) & out["close"].lt(fast)
        long = up & pull_long & resume_long
        short = down & pull_short & resume_short
        fire = out["rs_quality_ok"].eq(1) & out["rs_route_ok"].eq(1) & (long | short)
        out["tp1h_fast"] = fast
        out["tp1h_slow"] = slow
        out["tp1h_trend_ok"] = (up | down).astype(float)
        out["tp1h_pullback"] = (pull_long | pull_short).astype(float)
        out["tp1h_resume"] = (resume_long | resume_short).astype(float)
        out["tp1h_side"] = long.map({True: "long", False: "short"})
        out["tp1h_atr"] = atr(out, p.atr_period)
        out["tp1h_fire"] = fire.astype(float)
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index < self.warmup_bars or not bool(df.iloc[index]["tp1h_fire"]):
            return None
        row = df.iloc[index]
        side: Literal["long", "short"] = str(row["tp1h_side"])  # type: ignore[assignment]
        return _intent(side, float(row["close"]), float(row["tp1h_atr"]),
                       reward_r=self.params.reward_r,
                       reason=f"trend_pullback_1h_v1 {side} virtual_only")


@dataclass(frozen=True, slots=True)
class TickAcceptedBreakoutParams:
    range_bars: int = 24
    atr_period: int = 20
    buffer_bps: float = 5.0
    acceptance_seconds: float = 3.0
    min_samples: int = 3
    reward_r: float = 2.0


class TickAcceptedBreakoutV1(_DiagnosticScanner):
    """Bar arms; monotonically-timestamped ticks perform acceptance.

    ``observe_tick`` is intentionally pure observation and returns a virtual
    intent only after the frozen hold/sample contract.  The regular closed-bar
    ``signal`` returns None, preventing accidental bar-close substitution.
    """

    strategy_id = "tick_accepted_breakout_v1"
    eligibility = "RESEARCH_ONLY"
    timeframe = "5m"
    params = TickAcceptedBreakoutParams()
    warmup_bars = 25
    prefix = "tab"
    failure_contract = (
        ("rs_quality_ok", "data_quality_not_ok"),
        ("rs_route_ok", "regime_route_blocked"),
        ("tab_arm_ready", "breakout_not_armed"),
        ("tab_fire", "tick_acceptance_pending"),
    )
    threshold_contract: ClassVar[dict[str, float]] = {
        "acceptance_seconds": 3.0, "min_samples": 3.0, "buffer_bps": 5.0
    }

    def __init__(self, funding: pd.DataFrame | None = None) -> None:
        self.funding = funding
        self._side: Literal["long", "short"] | None = None
        self._level: float | None = None
        self._accepted_since: datetime | None = None
        self._samples = 0
        self._last_ts: datetime | None = None
        self._risk = 0.0

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out, _ = _frame(candles, name=self.strategy_id)
        p = self.params
        ph = prior_high(out["high"], p.range_bars) * (1 + p.buffer_bps / 10_000)
        pl = prior_low(out["low"], p.range_bars) * (1 - p.buffer_bps / 10_000)
        ready = (
            ph.notna() & pl.notna() & out["rs_quality_ok"].eq(1)
            & out["rs_route_ok"].eq(1)
        )
        out["tab_long_level"] = ph
        out["tab_short_level"] = pl
        out["tab_arm_ready"] = ready.astype(float)
        out["tab_atr"] = atr(out, p.atr_period)
        out["tab_fire"] = 0.0
        # Compatibility with the single canonical quote-acceptance runtime.
        # The geometry remains this strategy's prior-range hypothesis; these
        # aliases do not import squeeze logic or add a second tick path.
        out["sqz_episode"] = (ready & ~ready.shift(1, fill_value=False)).cumsum()
        out["sqz_range_high"] = ph
        out["sqz_range_low"] = pl
        out["sqz_atr"] = out["tab_atr"]
        out["sqz_vwap24"] = out["close"]
        out["sqz_compressed"] = ready.astype(float)
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index >= self.warmup_bars and index < len(df):
            row = df.iloc[index]
            if bool(row["tab_arm_ready"]):
                self._level = float(row["tab_long_level"])
                self._side = None
                self._accepted_since = None
                self._samples = 0
                self._risk = float(row["tab_atr"])
        return None

    def observe_tick(self, *, ts: datetime, price: float,
                     short_level: float | None = None) -> SignalIntent | None:
        if self._last_ts is not None and ts <= self._last_ts:
            raise ValueError("ticks must be strictly increasing")
        self._last_ts = ts
        long_level = self._level
        if long_level is None:
            return None
        side: Literal["long", "short"] | None = "long" if price > long_level else None
        if side is None and short_level is not None and price < short_level:
            side = "short"
            self._level = short_level
        if side is None or side != self._side:
            self._side = side
            self._accepted_since = ts if side else None
            self._samples = 1 if side else 0
            return None
        self._samples += 1
        assert self._accepted_since is not None
        held = (ts - self._accepted_since).total_seconds()
        if held < self.params.acceptance_seconds or self._samples < self.params.min_samples:
            return None
        intent = _intent(side, price, self._risk, reward_r=self.params.reward_r,
                         reason=f"tick_accepted_breakout_v1 {side} held={held:.1f}s virtual_only")
        self._side = None
        self._accepted_since = None
        self._samples = 0
        return intent


NEW_RESEARCH_SCANNERS = (
    AvwapReclaim15mV1,
    SessionContinuation15mV1,
    LiquiditySweepReversal15mV1,
    TrendPullback1hV1,
    TickAcceptedBreakoutV1,
)

__all__ = [item.__name__ for item in NEW_RESEARCH_SCANNERS] + ["NEW_RESEARCH_SCANNERS"]
