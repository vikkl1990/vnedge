"""Early range-expansion observer on closed 15-minute bars.

V2 waited for the expanding one-hour candle to close.  V3 preserves the
pre-registered UTC/session and same-hour-of-day context, but evaluates the
forming hour at each causal 15-minute close.  Quotes still price the virtual
entry and the runtime protects an accepted position between candle closes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

import pandas as pd

from vnedge.plan.cost_model import CostModel
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent


@dataclass(frozen=True, slots=True)
class RangeExpansionV3Params:
    prior_range_bars: int = 48
    volume_lookback: int = 96
    volume_mult: float = 1.2
    body_min_bps: float = 10.0
    break_buffer_bps: float = 5.0
    atr_period: int = 56
    atr_stop_mult: float = 1.5
    min_bars_between_signals: int = 48
    projected_reward_r: float = 2.0
    round_trip_cost_bps: float = CostModel.for_profile("swing").round_trip_bps()
    min_projected_net_bps: float = 20.0
    session_start_hour_utc: int = 12
    session_end_hour_utc: int = 16
    hour_profile_days: int = 20
    min_hour_range_mult: float = 1.2

    def __post_init__(self) -> None:
        if min(self.prior_range_bars, self.volume_lookback, self.atr_period) < 2:
            raise ValueError("lookback settings are invalid")
        if self.hour_profile_days < 5 or self.min_hour_range_mult <= 0:
            raise ValueError("hour profile settings are invalid")
        if not 0 <= self.session_start_hour_utc < self.session_end_hour_utc <= 24:
            raise ValueError("UTC session settings are invalid")


PARAMS: Final = RangeExpansionV3Params()
STRATEGY_SPEC = MappingProxyType(
    {
        "strategy_id": "range_expansion_observer_v3",
        "eligibility": "RESEARCH_ONLY",
        "capital_eligible": False,
        "tradeable": False,
        "timeframe": "15m",
        "params": PARAMS,
        "purpose": "15m-confirmed expansion inside a session-conditioned forming hour",
    }
)


class RangeExpansionObserverV3(BaseStrategy):
    strategy_id = "range_expansion_observer_v3"
    eligibility = "RESEARCH_ONLY"
    timeframe = "15m"
    params = PARAMS
    warmup_bars = max(
        PARAMS.prior_range_bars,
        PARAMS.volume_lookback,
        PARAMS.atr_period,
        (PARAMS.hour_profile_days + 1) * 24 * 4,
    ) + 1

    def __init__(self, funding: pd.DataFrame | None = None) -> None:
        self.funding = funding

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = required.difference(candles.columns)
        if missing:
            raise ValueError(f"range expansion v3 missing columns: {sorted(missing)}")
        out = candles.copy()
        ts = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
        if ts.isna().any():
            raise ValueError("range expansion v3 requires valid UTC timestamps")
        p = self.params
        open_ = pd.to_numeric(out["open"], errors="coerce")
        high = pd.to_numeric(out["high"], errors="coerce")
        low = pd.to_numeric(out["low"], errors="coerce")
        close = pd.to_numeric(out["close"], errors="coerce")
        volume = pd.to_numeric(out["volume"], errors="coerce")

        hour_open = ts.dt.floor("h")
        hour_key = pd.Series(hour_open, index=out.index)
        hour_high = high.groupby(hour_key).cummax()
        hour_low = low.groupby(hour_key).cummin()
        hour_first_open = open_.groupby(hour_key).transform("first")
        hour_range_so_far = (hour_high - hour_low).div(hour_first_open).mul(10_000)

        # The current hour's final value is deliberately excluded by shift(1).
        # Therefore its use to construct the historical profile cannot leak the
        # remaining 15-minute children of the current hour.
        hourly_input = pd.DataFrame(
            {
                "hour_open": hour_open,
                "minute": ts.dt.minute,
                "high": high,
                "low": low,
                "open": open_,
                "quality_ok": (
                    out["data_quality"].astype(str).str.lower().eq("ok")
                    if "data_quality" in out.columns
                    else True
                ),
            }
        ).groupby("hour_open", sort=True).agg(
            high=("high", "max"),
            low=("low", "min"),
            open=("open", "first"),
            children=("minute", "nunique"),
            quality_ok=("quality_ok", "all"),
        )
        hourly = hourly_input.loc[
            hourly_input["children"].eq(4) & hourly_input["quality_ok"]
        ].copy()
        hourly["range_bps"] = (hourly["high"] - hourly["low"]).div(
            hourly["open"]
        ).mul(10_000)
        hourly["hour"] = hourly.index.hour
        hourly["profile_bps"] = hourly["range_bps"].groupby(
            hourly["hour"]
        ).transform(
            lambda values: values.rolling(
                p.hour_profile_days, min_periods=p.hour_profile_days
            ).median()
        )
        profile_updates = pd.DataFrame(
            {
                "available_at": hourly.index + pd.Timedelta(hours=1),
                "hour": hourly["hour"].to_numpy(),
                "profile_bps": hourly["profile_bps"].to_numpy(),
            }
        ).dropna(subset=["profile_bps"])
        profile_requests = pd.DataFrame(
            {
                "_row": out.index,
                "decision_at": ts + pd.Timedelta(minutes=15),
                "hour": ts.dt.hour,
            }
        )
        if profile_updates.empty:
            hour_median = pd.Series(float("nan"), index=out.index)
        else:
            mapped = pd.merge_asof(
                profile_requests.sort_values("decision_at"),
                profile_updates.sort_values("available_at"),
                left_on="decision_at",
                right_on="available_at",
                by="hour",
                direction="backward",
            ).sort_values("_row")
            hour_median = mapped["profile_bps"].set_axis(out.index)

        prior_high = high.shift(1).rolling(
            p.prior_range_bars, min_periods=p.prior_range_bars
        ).max()
        prior_low = low.shift(1).rolling(
            p.prior_range_bars, min_periods=p.prior_range_bars
        ).min()
        volume_base = volume.shift(1).rolling(
            p.volume_lookback, min_periods=p.volume_lookback
        ).median()
        body_bps = (close - open_).abs().div(open_).mul(10_000)
        previous_close = close.shift(1)
        true_range = pd.concat(
            [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
            axis=1,
        ).max(axis=1)
        atr = true_range.shift(1).rolling(
            p.atr_period, min_periods=p.atr_period
        ).mean()
        session_ok = ts.dt.hour.ge(p.session_start_hour_utc) & ts.dt.hour.lt(
            p.session_end_hour_utc
        )
        expansion_ok = hour_range_so_far.ge(hour_median.mul(p.min_hour_range_mult))
        volume_ok = volume.ge(volume_base.mul(p.volume_mult))
        quality_ok = (
            out["data_quality"].astype(str).str.lower().eq("ok")
            if "data_quality" in out.columns
            else pd.Series(True, index=out.index)
        )
        common = (
            body_bps.ge(p.body_min_bps)
            & session_ok
            & expansion_ok
            & volume_ok
            & quality_ok
        )
        long_level = prior_high.mul(1 + p.break_buffer_bps / 10_000)
        short_level = prior_low.mul(1 - p.break_buffer_bps / 10_000)
        long_raw = previous_close.le(long_level) & close.gt(long_level) & common
        short_raw = previous_close.ge(short_level) & close.lt(short_level) & common

        fire_long = [False] * len(out)
        fire_short = [False] * len(out)
        last_fire = -(10**9)
        for index, (is_long, is_short) in enumerate(
            zip(long_raw.fillna(False), short_raw.fillna(False), strict=True)
        ):
            if index - last_fire < p.min_bars_between_signals:
                continue
            if is_long:
                fire_long[index] = True
                last_fire = index
            elif is_short:
                fire_short[index] = True
                last_fire = index

        out["rex3_prior_high"] = prior_high
        out["rex3_prior_low"] = prior_low
        out["rex3_volume_base"] = volume_base
        out["rex3_volume_ok"] = volume_ok.astype(float)
        out["rex3_body_bps"] = body_bps
        out["rex3_hour_range_bps"] = hour_range_so_far
        out["rex3_hour_median_bps"] = hour_median
        out["rex3_hour_range_ratio"] = hour_range_so_far.div(hour_median)
        out["rex3_session_ok"] = session_ok.astype(float)
        out["rex3_expansion_ok"] = expansion_ok.astype(float)
        out["rex3_atr"] = atr
        out["rex3_fire_long"] = pd.Series(fire_long, index=out.index).astype(float)
        out["rex3_fire_short"] = pd.Series(fire_short, index=out.index).astype(float)
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index < self.warmup_bars or index >= len(df):
            return None
        row = df.iloc[index]
        is_long = float(row["rex3_fire_long"]) > 0
        is_short = float(row["rex3_fire_short"]) > 0
        if not (is_long or is_short):
            return None
        close = float(row["close"])
        atr = float(row["rex3_atr"])
        if not math.isfinite(close) or not math.isfinite(atr) or close <= 0 or atr <= 0:
            return None
        risk = self.params.atr_stop_mult * atr
        projected_net = (
            risk / close * 10_000 * self.params.projected_reward_r
            - self.params.round_trip_cost_bps
        )
        if projected_net < self.params.min_projected_net_bps:
            return None
        side: Literal["long", "short"] = "long" if is_long else "short"
        stop = close - risk if side == "long" else close + risk
        target = (
            close + self.params.projected_reward_r * risk
            if side == "long"
            else close - self.params.projected_reward_r * risk
        )
        if stop <= 0 or target <= 0:
            return None
        return SignalIntent(
            side=side,
            stop_price=stop,
            take_profit_price=target,
            reason=(
                f"range_expansion_v3 side={side} confirmation=15m "
                f"hour_ratio={float(row['rex3_hour_range_ratio']):.2f} "
                f"projected_net={projected_net:.1f}bps session=12-16UTC virtual_only"
            ),
        )


__all__ = ["PARAMS", "STRATEGY_SPEC", "RangeExpansionObserverV3", "RangeExpansionV3Params"]
