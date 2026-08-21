"""Squeeze -> expansion breakout observer (compression-gated, both sides).

Detects the rare compression -> expansion ignition: a multi-hour range whose
width ranks in the bottom quintile of the trailing ~7 days, broken with
volume confirmation.  Fires long and short.  Designed to stay silent in chop:
one signal per compression episode, a minimum spacing between signals, and a
minimum modeled edge after Delta all-in taker cost.

Spec lineage: operator spec ``squeeze_expansion_breakout_v2`` (2026-08-18),
amended in two reviewed ways before registration:

- ``atr_proxy = range_N / N`` from the draft spec is replaced by a true
  ATR(``atr_period``); the draft formula yields sub-spread stops and zero
  qualifying arms on any tape tested.
- compression is required on the bar *before* the breakout bar
  (``shift(1)``), never the breakout bar itself -- the panic_reversal
  post-mortem showed same-bar conjunctions produce zero setups.

Outcome-dependent runtime behaviour from the spec (cooldown after loss/win,
UTC-midnight arm budget reset) belongs to the runtime arm-state layer, not to
this causal strategy; the causal constraints implemented here are the
per-episode single fire and the minimum bar spacing.

All calculations are causal and run on closed 5-minute bars.  RESEARCH_ONLY:
virtual intents for shadow observation; capital permission is impossible
without a separate reviewed registry change.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

import pandas as pd

from vnedge.plan.cost_model import CostModel
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent

_CONSERVATIVE_RT_COST_BPS: Final = CostModel.for_profile(
    "delta_scalp"
).round_trip_bps()


@dataclass(frozen=True, slots=True)
class SqueezeExpansionParams:
    """Frozen research registration; changes require a new strategy ID."""

    compression_bars: int = 48
    rank_lookback_bars: int = 2016
    compression_threshold: float = 0.20
    volume_lookback: int = 48
    min_volume_mult: float = 1.3
    atr_period: int = 48
    atr_stop_mult: float = 1.7
    reward_r: float = 2.3
    round_trip_cost_bps: float = _CONSERVATIVE_RT_COST_BPS
    min_edge_after_cost_bps: float = 20.0
    min_bars_between_signals: int = 18
    break_buffer_bps: float = 2.0

    def __post_init__(self) -> None:
        if self.compression_bars < 2 or self.rank_lookback_bars <= self.compression_bars:
            raise ValueError("compression windows are invalid")
        if not 0 < self.compression_threshold < 1:
            raise ValueError("compression threshold must be a percentile in (0, 1)")
        if self.volume_lookback < 2 or self.min_volume_mult <= 0:
            raise ValueError("volume confirmation settings are invalid")
        if self.atr_period < 2 or self.atr_stop_mult <= 0 or self.reward_r <= 0:
            raise ValueError("stop geometry settings are invalid")
        if self.round_trip_cost_bps < 0 or self.min_edge_after_cost_bps <= 0:
            raise ValueError("cost settings are invalid")
        if self.min_bars_between_signals < 1 or self.break_buffer_bps < 0:
            raise ValueError("spacing settings are invalid")


PARAMS: Final = SqueezeExpansionParams()

STRATEGY_SPEC = MappingProxyType(
    {
        "strategy_id": "squeeze_expansion_breakout_v2",
        "eligibility": "RESEARCH_ONLY",
        "capital_eligible": False,
        "tradeable": False,
        "timeframe": "5m",
        "params": PARAMS,
        "purpose": "compression->expansion ignition measurement, long and short",
    }
)


class SqueezeExpansionBreakout(BaseStrategy):
    """One virtual intent per compression episode when its range breaks."""

    strategy_id = "squeeze_expansion_breakout_v2"
    eligibility = "RESEARCH_ONLY"
    timeframe = "5m"
    params = PARAMS
    warmup_bars = PARAMS.rank_lookback_bars + PARAMS.compression_bars + 1

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        params: SqueezeExpansionParams | None = None,
    ) -> None:
        selected = params or PARAMS
        if selected != PARAMS:
            raise ValueError(
                "squeeze_expansion_breakout_v2 params are frozen; use a new strategy ID"
            )
        self.funding = funding
        self.params = selected

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        required = {"high", "low", "close", "volume"}
        missing = required.difference(candles.columns)
        if missing:
            raise ValueError(f"squeeze breakout missing candle columns: {sorted(missing)}")

        p = self.params
        out = candles.copy()
        close = pd.to_numeric(out["close"], errors="coerce")
        high = pd.to_numeric(out["high"], errors="coerce")
        low = pd.to_numeric(out["low"], errors="coerce")
        volume = pd.to_numeric(out["volume"], errors="coerce")

        # Compression state of the *prior* N bars (current bar excluded so the
        # breakout bar can never be part of its own compression measurement).
        range_high = high.shift(1).rolling(p.compression_bars, min_periods=p.compression_bars).max()
        range_low = low.shift(1).rolling(p.compression_bars, min_periods=p.compression_bars).min()
        range_pct = (range_high - range_low).div(close.shift(1))
        range_rank = range_pct.rolling(
            p.rank_lookback_bars, min_periods=p.rank_lookback_bars
        ).rank(pct=True)
        compressed = range_rank.le(p.compression_threshold)

        prev_close = close.shift(1)
        true_range = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        atr = true_range.shift(1).rolling(p.atr_period, min_periods=p.atr_period).mean()

        vol_ma = volume.shift(1).rolling(p.volume_lookback, min_periods=p.volume_lookback).mean()
        volume_ok = volume.gt(vol_ma.mul(p.min_volume_mult))

        buffer = close.shift(1).mul(p.break_buffer_bps / 10_000)
        raw_long = compressed & close.gt(range_high.add(buffer)) & volume_ok
        raw_short = compressed & close.lt(range_low.sub(buffer)) & volume_ok

        # One fire per compression episode + minimum spacing between fires.
        # Plain backward loop: state depends only on rows at or before i.
        episode = compressed.ne(compressed.shift(1, fill_value=False)).cumsum()
        fire_long = [False] * len(out)
        fire_short = [False] * len(out)
        last_fire = -(10**9)
        fired_episode: object = None
        raw_long_list = raw_long.fillna(False).tolist()
        raw_short_list = raw_short.fillna(False).tolist()
        episode_list = episode.tolist()
        for i in range(len(out)):
            if not (raw_long_list[i] or raw_short_list[i]):
                continue
            if i - last_fire < p.min_bars_between_signals:
                continue
            if episode_list[i] == fired_episode:
                continue
            if raw_long_list[i]:
                fire_long[i] = True
            else:
                fire_short[i] = True
            last_fire = i
            fired_episode = episode_list[i]

        # Extra causal columns for the trigger/exit-plane observer runner.
        episode = compressed.ne(compressed.shift(1, fill_value=False)).cumsum()
        pv = (close * volume).shift(1).rolling(288, min_periods=288).sum()
        vv = volume.shift(1).rolling(288, min_periods=288).sum()
        out["sqz_compressed"] = compressed.astype(float)
        out["sqz_episode"] = episode.astype(float)
        out["sqz_vol_ma"] = vol_ma
        out["sqz_vwap24"] = pv.div(vv.where(vv > 0))
        out["sqz_range_rank"] = range_rank
        out["sqz_range_high"] = range_high
        out["sqz_range_low"] = range_low
        out["sqz_atr"] = atr
        out["sqz_volume_ok"] = volume_ok.astype(float)
        out["sqz_fire_long"] = pd.Series(fire_long, index=out.index).astype(float)
        out["sqz_fire_short"] = pd.Series(fire_short, index=out.index).astype(float)
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index <= 0 or index >= len(df) or index < self.warmup_bars:
            return None
        row = df.iloc[index]
        fire_long = float(row["sqz_fire_long"]) > 0
        fire_short = float(row["sqz_fire_short"]) > 0
        if not (fire_long or fire_short):
            return None

        p = self.params
        close = float(row["close"])
        atr = float(row["sqz_atr"])
        rank = float(row["sqz_range_rank"])
        if not math.isfinite(close) or close <= 0 or not math.isfinite(atr) or atr <= 0:
            return None

        risk = p.atr_stop_mult * atr
        risk_bps = risk / close * 10_000
        reward_bps = risk_bps * p.reward_r
        if reward_bps - p.round_trip_cost_bps < p.min_edge_after_cost_bps:
            return None

        side: Literal["long", "short"] = "long" if fire_long else "short"
        if side == "long":
            stop = close - risk
            target = close + risk * p.reward_r
        else:
            stop = close + risk
            target = close - risk * p.reward_r
        if stop <= 0 or target <= 0:
            return None
        return SignalIntent(
            side=side,
            stop_price=stop,
            take_profit_price=target,
            reason=(
                f"squeeze_expansion side={side} rank={rank:.2f} "
                f"risk={risk_bps:.1f}bps reward={reward_bps:.1f}bps "
                f"edge_after_cost={reward_bps - p.round_trip_cost_bps:.1f}bps virtual_only"
            ),
        )
