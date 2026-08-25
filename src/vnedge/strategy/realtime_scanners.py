"""Quote-triggered successors for the active closed-bar scanners.

These are deliberately new strategy IDs.  The historical range-v4, BoS-v3,
and session-v1 registrations remain immutable and replayable.  Their
successors use closed candles only to ARM levels; a fresh top-of-book stream
must then hold beyond the level before a virtual entry is accepted.

All registrations remain RESEARCH_ONLY / SHADOW_OBSERVE and cannot place
orders or acquire capital permission.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

import pandas as pd

from vnedge.plan.cost_model import CostModel
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent, StrategyExitIntent
from vnedge.strategy.range_expansion_observer_v4 import (
    RangeExpansionObserverV4,
)
from vnedge.strategy.realtime_entry import RealtimeEntryArm
from vnedge.strategy.research_scanners import SessionContinuation15mV1
from vnedge.strategy.squeeze_expansion_breakout_v3 import SqueezeExpansionV3Params
from vnedge.strategy.structure_bos_15m_trigger_v3 import StructureBos15mTriggerV3


def _episode(row: pd.Series, index: int) -> int:
    ts = pd.Timestamp(row["timestamp"])
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.timestamp() * 1000) + index


def _finite_positive(*values: float) -> bool:
    return all(math.isfinite(value) and value > 0 for value in values)


class _QuoteEntryOnly:
    """Mixin that makes dual bar/quote entry structurally impossible."""

    realtime_fixed_target = True

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        del df, index
        return None


RANGE_ACCEPTANCE: Final = SqueezeExpansionV3Params(
    arm_grace_bars=1,
    acceptance_hold_seconds=3.0,
    min_acceptance_samples=3,
    max_chase_bps=10.0,
    break_buffer_bps=0.0,
    max_probes_per_side=2,
    max_fires_per_side=1,
    max_fires_per_day=2,
    cooldown_loss_bars=48,
    cooldown_win_bars=48,
    atr_stop_mult=1.5,
)


class RangeExpansionRealtimeV1(_QuoteEntryOnly, RangeExpansionObserverV4):
    """Expansion context on closed 15m; breakout acceptance on quotes."""

    strategy_id = "range_expansion_realtime_v1"
    acceptance_params = RANGE_ACCEPTANCE
    realtime_reward_r = float(RangeExpansionObserverV4.params.projected_reward_r)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out = super().prepare(candles)
        p = self.params
        close = pd.to_numeric(out["close"], errors="coerce")
        long_level = pd.to_numeric(out["rex3_prior_high"], errors="coerce").mul(
            1 + p.break_buffer_bps / 10_000
        )
        short_level = pd.to_numeric(out["rex3_prior_low"], errors="coerce").mul(
            1 - p.break_buffer_bps / 10_000
        )
        common = (
            out["rex3_quality_ok"].eq(1)
            & out["rex3_session_ok"].eq(1)
            & out["rex3_expansion_ok"].eq(1)
            & out["rex3_volume_ok"].eq(1)
            & out["rex3_body_bps"].ge(p.body_min_bps)
            & out["rex3_projected_net_bps"].ge(p.min_projected_net_bps)
        )
        allow_long = common & close.le(long_level)
        allow_short = common & close.ge(short_level)
        out["rt_long_level"] = long_level
        out["rt_short_level"] = short_level
        out["rt_allow_long"] = allow_long.astype(float)
        out["rt_allow_short"] = allow_short.astype(float)
        out["rt_atr"] = out["rex3_atr"]
        out["rt_arm_ready"] = (allow_long | allow_short).astype(float)
        return out

    def realtime_arm(self, df: pd.DataFrame, index: int) -> RealtimeEntryArm | None:
        if index < self.warmup_bars or index >= len(df):
            return None
        row = df.iloc[index]
        if not bool(row.get("rt_arm_ready", 0)):
            return None
        long_level = float(row["rt_long_level"])
        short_level = float(row["rt_short_level"])
        atr = float(row["rt_atr"])
        close = float(row["close"])
        if not _finite_positive(long_level, short_level, atr, close):
            return None
        return RealtimeEntryArm(
            episode_id=_episode(row, index),
            bar_index=index,
            long_level=long_level,
            short_level=short_level,
            atr=atr,
            reference_price=close,
            allow_long=bool(row["rt_allow_long"]),
            allow_short=bool(row["rt_allow_short"]),
            expires_after_bars=1,
            session_start_hour_utc=self.params.session_start_hour_utc,
            session_end_hour_utc=self.params.session_end_hour_utc,
            reason=self.strategy_id,
        )


BOS_ACCEPTANCE: Final = SqueezeExpansionV3Params(
    arm_grace_bars=1,
    acceptance_hold_seconds=3.0,
    min_acceptance_samples=3,
    max_chase_bps=10.0,
    break_buffer_bps=0.0,
    max_probes_per_side=2,
    max_fires_per_side=1,
    max_fires_per_day=2,
    cooldown_loss_bars=48,
    cooldown_win_bars=48,
    atr_stop_mult=1.5,
)


class StructureBosRealtimeV1(_QuoteEntryOnly, StructureBos15mTriggerV3):
    """Confirmed 1h/4h structure arms one side; quotes trigger the break."""

    strategy_id = "structure_bos_realtime_v1"
    acceptance_params = BOS_ACCEPTANCE
    realtime_reward_r = float(StructureBos15mTriggerV3.params.reward_r)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out = super().prepare(candles)
        p = self.params
        close = pd.to_numeric(out["close"], errors="coerce")
        high = pd.to_numeric(out["bos15_last_swing_high"], errors="coerce")
        low = pd.to_numeric(out["bos15_last_swing_low"], errors="coerce")
        long_level = high.mul(1 + p.break_buffer_bps / 10_000)
        short_level = low.mul(1 - p.break_buffer_bps / 10_000)
        trend = out["bos15_structure_trend"].astype(str)
        htf = out["bos15_htf_structure_trend"].astype(str)
        bias = out["bos15_dual_avwap_bias"].astype(str)
        common = (
            out["bos15_structure_ready"].fillna(False).astype(bool)
            & out["bos15_quality_ok"].eq(1)
            & out["bos15_session_ok"].eq(1)
            & out["bos15_volume_ok"].eq(1)
        )
        allow_long = (
            common
            & trend.eq("up")
            & htf.eq("up")
            & ~bias.eq("strong_short")
            & close.le(long_level)
            & out["bos15_projected_net_long_bps"].ge(p.min_projected_net_bps)
        )
        allow_short = (
            common
            & trend.eq("down")
            & htf.eq("down")
            & ~bias.eq("strong_long")
            & close.ge(short_level)
            & out["bos15_projected_net_short_bps"].ge(p.min_projected_net_bps)
        )
        out["rt_long_level"] = long_level
        out["rt_short_level"] = short_level
        out["rt_long_structural_stop"] = low.mul(1 - p.stop_buffer_bps / 10_000)
        out["rt_short_structural_stop"] = high.mul(1 + p.stop_buffer_bps / 10_000)
        out["rt_allow_long"] = allow_long.astype(float)
        out["rt_allow_short"] = allow_short.astype(float)
        out["rt_atr"] = out["bos15_bos_atr"]
        out["rt_arm_ready"] = (allow_long | allow_short).astype(float)
        return out

    def realtime_arm(self, df: pd.DataFrame, index: int) -> RealtimeEntryArm | None:
        if index < self.warmup_bars or index >= len(df):
            return None
        row = df.iloc[index]
        if not bool(row.get("rt_arm_ready", 0)):
            return None
        long_level = float(row["rt_long_level"])
        short_level = float(row["rt_short_level"])
        atr = float(row["rt_atr"])
        close = float(row["close"])
        if not _finite_positive(long_level, short_level, atr, close):
            return None
        return RealtimeEntryArm(
            episode_id=_episode(row, index),
            bar_index=index,
            long_level=long_level,
            short_level=short_level,
            atr=atr,
            reference_price=close,
            allow_long=bool(row["rt_allow_long"]),
            allow_short=bool(row["rt_allow_short"]),
            long_structural_stop=float(row["rt_long_structural_stop"]),
            short_structural_stop=float(row["rt_short_structural_stop"]),
            expires_after_bars=1,
            session_start_hour_utc=self.params.session_start_hour_utc,
            session_end_hour_utc=self.params.session_end_hour_utc,
            reason=self.strategy_id,
        )


SESSION_ACCEPTANCE: Final = SqueezeExpansionV3Params(
    arm_grace_bars=1,
    acceptance_hold_seconds=3.0,
    min_acceptance_samples=3,
    max_chase_bps=8.0,
    break_buffer_bps=0.0,
    max_probes_per_side=2,
    max_fires_per_side=1,
    max_fires_per_day=1,
    cooldown_loss_bars=32,
    cooldown_win_bars=32,
    atr_stop_mult=1.0,
)


class SessionContinuationRealtimeV1(_QuoteEntryOnly, SessionContinuation15mV1):
    """Session context arms once; live quote acceptance triggers continuation."""

    strategy_id = "session_continuation_realtime_v1"
    acceptance_params = SESSION_ACCEPTANCE
    realtime_reward_r = float(SessionContinuation15mV1.params.reward_r)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out = super().prepare(candles)
        p = self.params
        high = (
            pd.to_numeric(out["high"], errors="coerce")
            .shift(1)
            .rolling(p.range_bars, min_periods=p.range_bars)
            .max()
        )
        low = (
            pd.to_numeric(out["low"], errors="coerce")
            .shift(1)
            .rolling(p.range_bars, min_periods=p.range_bars)
            .min()
        )
        close = pd.to_numeric(out["close"], errors="coerce")
        common = (
            out["rs_quality_ok"].eq(1)
            & out["rs_route_ok"].eq(1)
            & out["sc15_session_ok"].eq(1)
            & out["sc15_volume_ok"].eq(1)
        )
        allow_long = common & close.le(high)
        allow_short = common & close.ge(low)
        out["rt_long_level"] = high
        out["rt_short_level"] = low
        out["rt_allow_long"] = allow_long.astype(float)
        out["rt_allow_short"] = allow_short.astype(float)
        out["rt_atr"] = out["sc15_atr"]
        out["rt_arm_ready"] = (allow_long | allow_short).astype(float)
        return out

    def realtime_arm(self, df: pd.DataFrame, index: int) -> RealtimeEntryArm | None:
        if index < self.warmup_bars or index >= len(df):
            return None
        row = df.iloc[index]
        if not bool(row.get("rt_arm_ready", 0)):
            return None
        long_level = float(row["rt_long_level"])
        short_level = float(row["rt_short_level"])
        atr = float(row["rt_atr"])
        close = float(row["close"])
        if not _finite_positive(long_level, short_level, atr, close):
            return None
        return RealtimeEntryArm(
            episode_id=_episode(row, index),
            bar_index=index,
            long_level=long_level,
            short_level=short_level,
            atr=atr,
            reference_price=close,
            allow_long=bool(row["rt_allow_long"]),
            allow_short=bool(row["rt_allow_short"]),
            expires_after_bars=1,
            session_start_hour_utc=self.params.start_hour,
            session_end_hour_utc=self.params.end_hour,
            reason=self.strategy_id,
        )


@dataclass(frozen=True, slots=True)
class HtfStructureContinuationParams:
    """Frozen first revision of the structure-led continuation hypothesis."""

    fast_ema_bars: int = 20
    slow_ema_bars: int = 50
    atr_bars: int = 14
    volume_lookback: int = 96
    volume_mult: float = 0.8
    pullback_tolerance_atr: float = 0.35
    min_reclaim_body_bps: float = 4.0
    entry_buffer_bps: float = 2.0
    stop_buffer_bps: float = 4.0
    stop_atr_mult: float = 1.25
    edge_hypothesis_r: float = 2.5
    min_projected_net_bps: float = 8.0

    def __post_init__(self) -> None:
        if not 2 <= self.fast_ema_bars < self.slow_ema_bars:
            raise ValueError("HTF continuation EMA windows are invalid")
        if self.atr_bars < 2 or self.volume_lookback < 2:
            raise ValueError("HTF continuation lookbacks are invalid")
        if self.volume_mult <= 0 or self.pullback_tolerance_atr < 0:
            raise ValueError("HTF continuation setup thresholds are invalid")
        if self.stop_atr_mult <= 0 or self.edge_hypothesis_r <= 0:
            raise ValueError("HTF continuation risk geometry is invalid")


HTF_CONTINUATION_PARAMS: Final = HtfStructureContinuationParams()
HTF_CONTINUATION_ACCEPTANCE: Final = SqueezeExpansionV3Params(
    arm_grace_bars=2,
    acceptance_hold_seconds=3.0,
    min_acceptance_samples=3,
    max_chase_bps=8.0,
    break_buffer_bps=0.0,
    max_probes_per_side=2,
    max_fires_per_side=1,
    max_fires_per_day=2,
    cooldown_loss_bars=8,
    cooldown_win_bars=16,
    atr_stop_mult=HTF_CONTINUATION_PARAMS.stop_atr_mult,
)


class HtfStructureContinuationRealtimeV1(_QuoteEntryOnly, BaseStrategy):
    """4h/1h direction, 15m pullback/reclaim, live quote continuation.

    The scanner is deliberately asymmetric: only the higher-timeframe side
    can arm.  A closed 15m bar must pull into the fast EMA, reject it with a
    meaningful body, remain on the correct side of the slow EMA, and carry
    non-trivial volume.  Quotes then have to accept beyond that setup bar.

    There is no fixed profit cap.  Protection is structure/ATR based; the
    generic exit engine locks only after meaningful progress and trails the
    developed move.  Two closes through the slow EMA or a confirmed 1h/4h
    structure flip is deterioration, not an instruction to reverse.
    """

    strategy_id = "htf_structure_continuation_realtime_v1"
    eligibility = "RESEARCH_ONLY"
    timeframe = "15m"
    params = HTF_CONTINUATION_PARAMS
    acceptance_params = HTF_CONTINUATION_ACCEPTANCE
    warmup_bars = max(StructureBos15mTriggerV3.warmup_bars, 224)
    realtime_fixed_target = False
    realtime_failed_breakout = False
    realtime_breakeven_arm_r = 1.25
    realtime_trail_arm_r = 2.0
    realtime_trail_atr_mult = 1.5

    def __init__(self, funding: pd.DataFrame | None = None) -> None:
        self.funding = funding

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        # Build the frozen BoS context with its own parameter object.  This
        # scanner's public ``params`` are the pullback contract below, so the
        # workflow UI never reports the inherited BoS thresholds by mistake.
        out = StructureBos15mTriggerV3(self.funding).prepare(candles)
        p = self.params
        open_ = pd.to_numeric(out["open"], errors="coerce")
        high = pd.to_numeric(out["high"], errors="coerce")
        low = pd.to_numeric(out["low"], errors="coerce")
        close = pd.to_numeric(out["close"], errors="coerce")
        volume = pd.to_numeric(out["volume"], errors="coerce")
        previous_close = close.shift(1)
        true_range = pd.concat(
            [
                high - low,
                (high - previous_close).abs(),
                (low - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = true_range.rolling(p.atr_bars, min_periods=p.atr_bars).mean()
        fast = close.ewm(span=p.fast_ema_bars, adjust=False, min_periods=p.fast_ema_bars).mean()
        slow = close.ewm(span=p.slow_ema_bars, adjust=False, min_periods=p.slow_ema_bars).mean()
        volume_base = (
            volume.shift(1).rolling(p.volume_lookback, min_periods=p.volume_lookback).median()
        )
        volume_ratio = volume / volume_base
        body_bps = (close - open_).abs() / close * 10_000
        trend = out["bos15_structure_trend"].astype(str)
        htf = out["bos15_htf_structure_trend"].astype(str)
        bias = out["bos15_dual_avwap_bias"].astype(str)
        quality = out["bos15_quality_ok"].eq(1)
        ready = out["bos15_structure_ready"].fillna(False).astype(bool)
        meaningful = volume_ratio.ge(p.volume_mult) & body_bps.ge(p.min_reclaim_body_bps)
        long_context = ready & quality & trend.eq("up") & htf.eq("up") & ~bias.eq("strong_short")
        short_context = (
            ready & quality & trend.eq("down") & htf.eq("down") & ~bias.eq("strong_long")
        )
        long_pullback = (
            low.le(fast + p.pullback_tolerance_atr * atr)
            & close.gt(fast)
            & close.gt(slow)
            & close.gt(open_)
        )
        short_pullback = (
            high.ge(fast - p.pullback_tolerance_atr * atr)
            & close.lt(fast)
            & close.lt(slow)
            & close.lt(open_)
        )
        long_level = high.mul(1 + p.entry_buffer_bps / 10_000)
        short_level = low.mul(1 - p.entry_buffer_bps / 10_000)
        long_structural_stop = low.mul(1 - p.stop_buffer_bps / 10_000)
        short_structural_stop = high.mul(1 + p.stop_buffer_bps / 10_000)
        long_stop = pd.concat(
            [
                long_level - p.stop_atr_mult * atr,
                long_structural_stop,
            ],
            axis=1,
        ).min(axis=1, skipna=False)
        short_stop = pd.concat(
            [
                short_level + p.stop_atr_mult * atr,
                short_structural_stop,
            ],
            axis=1,
        ).max(axis=1, skipna=False)
        # The strategy object is venue-neutral at preparation time. Use the
        # highest enabled swing gate so a Delta arm cannot be created from the
        # cheaper Binance tariff; the runtime CostGate still applies the exact
        # venue profile before approving a virtual intent.
        round_trip = max(
            CostModel.for_profile("swing").round_trip_bps(),
            CostModel.for_profile("delta_swing").round_trip_bps(),
        )
        projected_long = (
            long_level - long_stop
        ) / long_level * 10_000 * p.edge_hypothesis_r - round_trip
        projected_short = (
            short_stop - short_level
        ) / short_level * 10_000 * p.edge_hypothesis_r - round_trip
        allow_long = (
            long_context & meaningful & long_pullback & projected_long.ge(p.min_projected_net_bps)
        )
        allow_short = (
            short_context
            & meaningful
            & short_pullback
            & projected_short.ge(p.min_projected_net_bps)
        )
        out["hsc_fast_ema"] = fast
        out["hsc_slow_ema"] = slow
        out["hsc_atr"] = atr
        out["hsc_volume_ratio"] = volume_ratio
        out["hsc_body_bps"] = body_bps
        out["hsc_htf_aligned_long"] = long_context.astype(float)
        out["hsc_htf_aligned_short"] = short_context.astype(float)
        out["hsc_pullback_long"] = long_pullback.astype(float)
        out["hsc_pullback_short"] = short_pullback.astype(float)
        out["hsc_projected_net_long_bps"] = projected_long
        out["hsc_projected_net_short_bps"] = projected_short
        out["rt_long_level"] = long_level
        out["rt_short_level"] = short_level
        out["rt_long_structural_stop"] = long_structural_stop
        out["rt_short_structural_stop"] = short_structural_stop
        out["rt_allow_long"] = allow_long.astype(float)
        out["rt_allow_short"] = allow_short.astype(float)
        out["rt_atr"] = atr
        out["rt_arm_ready"] = (allow_long | allow_short).astype(float)
        return out

    def realtime_arm(self, df: pd.DataFrame, index: int) -> RealtimeEntryArm | None:
        if index < self.warmup_bars or index >= len(df):
            return None
        row = df.iloc[index]
        if not bool(row.get("rt_arm_ready", 0)):
            return None
        long_level = float(row["rt_long_level"])
        short_level = float(row["rt_short_level"])
        atr = float(row["rt_atr"])
        close = float(row["close"])
        if not _finite_positive(long_level, short_level, atr, close):
            return None
        return RealtimeEntryArm(
            episode_id=_episode(row, index),
            bar_index=index,
            long_level=long_level,
            short_level=short_level,
            atr=atr,
            reference_price=close,
            allow_long=bool(row["rt_allow_long"]),
            allow_short=bool(row["rt_allow_short"]),
            long_structural_stop=float(row["rt_long_structural_stop"]),
            short_structural_stop=float(row["rt_short_structural_stop"]),
            structural_stop_mode="structure_floor",
            expires_after_bars=2,
            reason=self.strategy_id,
        )

    def exit_signal(
        self,
        df: pd.DataFrame,
        index: int,
        side: str,
        entry_price: float,
    ) -> StrategyExitIntent | None:
        del entry_price
        if index < 1 or index >= len(df):
            return None
        row = df.iloc[index]
        previous = df.iloc[index - 1]
        close = float(row["close"])
        previous_close = float(previous["close"])
        slow = float(row["hsc_slow_ema"])
        previous_slow = float(previous["hsc_slow_ema"])
        if not all(
            math.isfinite(value) and value > 0
            for value in (close, previous_close, slow, previous_slow)
        ):
            return None
        trend = str(row.get("bos15_structure_trend", "unavailable"))
        htf = str(row.get("bos15_htf_structure_trend", "unavailable"))
        if side == "long":
            deteriorated = (
                trend == "down"
                or htf == "down"
                or (close < slow and previous_close < previous_slow)
            )
        elif side == "short":
            deteriorated = (
                trend == "up" or htf == "up" or (close > slow and previous_close > previous_slow)
            )
        else:
            return None
        if not deteriorated:
            return None
        return StrategyExitIntent(reason="htf_structure_deterioration")

    def evaluation_diagnostics(self, df: pd.DataFrame, index: int) -> dict[str, object]:
        row = df.iloc[index]

        def number(name: str) -> float | None:
            try:
                value = float(row.get(name, float("nan")))
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) else None

        def flag(name: str) -> bool:
            return bool(number(name) or 0)

        long_context = flag("hsc_htf_aligned_long")
        short_context = flag("hsc_htf_aligned_short")
        aligned = long_context or short_context
        pullback = flag("hsc_pullback_long") if long_context else flag("hsc_pullback_short")
        volume_ratio = number("hsc_volume_ratio")
        body_bps = number("hsc_body_bps")
        projected = (
            number("hsc_projected_net_long_bps")
            if long_context
            else number("hsc_projected_net_short_bps")
        )
        p = self.params
        volume_ok = volume_ratio is not None and volume_ratio >= p.volume_mult
        body_ok = body_bps is not None and body_bps >= p.min_reclaim_body_bps
        edge_ok = projected is not None and projected >= p.min_projected_net_bps
        failures = [
            reason
            for ok, reason in (
                (aligned, "no_htf_direction"),
                (pullback, "no_meaningful_pullback_reclaim"),
                (volume_ok, "volume_below_setup_floor"),
                (body_ok, "reclaim_body_too_small"),
                (edge_ok, "payoff_hypothesis_below_threshold"),
            )
            if not ok
        ]
        eligible = flag("rt_arm_ready")
        return {
            "eligible": eligible,
            "primary_failed_gate": failures[0] if failures else None,
            "all_failed_gates": failures,
            "features": {
                "htf_4h": str(row.get("bos15_htf_structure_trend", "unavailable")),
                "structure_1h": str(row.get("bos15_structure_trend", "unavailable")),
                "dual_avwap_bias": str(row.get("bos15_dual_avwap_bias", "unavailable")),
                "pullback_reclaim": pullback,
                "volume_ratio": volume_ratio,
                "body_bps": body_bps,
                "payoff_hypothesis_after_cost_bps": projected,
                "payoff_hypothesis_basis": "stop_distance_x_reward_r_minus_cost_not_expected_ev",
                "entry_mode": "live_quote_hold",
            },
            "thresholds": {
                "volume_mult": p.volume_mult,
                "min_reclaim_body_bps": p.min_reclaim_body_bps,
                "stop_atr_mult": p.stop_atr_mult,
                "edge_hypothesis_r": p.edge_hypothesis_r,
                "min_payoff_hypothesis_after_cost_bps": p.min_projected_net_bps,
            },
        }


REALTIME_SCANNERS = (
    RangeExpansionRealtimeV1,
    StructureBosRealtimeV1,
    SessionContinuationRealtimeV1,
    HtfStructureContinuationRealtimeV1,
)

STRATEGY_SPECS = tuple(
    MappingProxyType(
        {
            "strategy_id": strategy.strategy_id,
            "eligibility": "RESEARCH_ONLY",
            "capital_eligible": False,
            "tradeable": False,
            "timeframe": "15m",
            "purpose": "closed-bar setup with current-quote entry acceptance",
        }
    )
    for strategy in REALTIME_SCANNERS
)

__all__ = [strategy.__name__ for strategy in REALTIME_SCANNERS] + [
    "REALTIME_SCANNERS",
    "STRATEGY_SPECS",
]
