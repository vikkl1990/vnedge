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
import struct
from dataclasses import dataclass, replace
from hashlib import sha256
from types import MappingProxyType
from typing import Final

import pandas as pd

from vnedge.data.candles import Candle
from vnedge.plan.cost_model import CostModel
from vnedge.strategy.arm_evidence import MissingHtfContext
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent, StrategyExitIntent
from vnedge.strategy.range_expansion_observer_v4 import (
    RangeExpansionObserverV4,
)
from vnedge.strategy.realtime_entry import RealtimeEntryArm
from vnedge.strategy.research_scanners import SessionContinuation15mV1
from vnedge.strategy.squeeze_expansion_breakout_v3 import SqueezeExpansionV3Params
from vnedge.strategy.structure_bos_15m_trigger_v3 import StructureBos15mTriggerV3


def _episode(row: pd.Series, index: int) -> int:
    """Return a replay-stable identity for a decision candle.

    ``index`` is deliberately ignored.  A bounded replay and a long-running
    lane can place the same canonical candle at different DataFrame offsets;
    including that offset made otherwise-identical quote intents disagree and
    could replenish an acceptance probe after a restart.  The canonical candle
    timestamp is already unique within one strategy/symbol/timeframe lane.
    """
    del index
    ts = pd.Timestamp(row["timestamp"])
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return int(ts.timestamp() * 1000)


def _hour_episode(row: pd.Series) -> int:
    """Stable identifier for one hourly range-expansion hypothesis.

    Range arms refresh every 15 minutes, but a chased/burned breakout remains
    the same economic move until the hour changes.  A per-row episode let the
    next candle silently resurrect a setup already rejected as too late.
    """
    ts = pd.Timestamp(row["timestamp"])
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return int(ts.floor("h").timestamp() * 1000)


def _level_episode(long_level: float, short_level: float) -> int:
    """Stable identity for one structural boundary pair.

    A confirmed swing remains the same economic hypothesis across several
    15-minute refreshes.  Row timestamps must not silently create a fresh
    probe budget for the same swing on every close.
    """
    payload = struct.pack("!dd", long_level, short_level)
    return int.from_bytes(sha256(payload).digest()[:8], "big", signed=False)


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
            episode_id=_hour_episode(row),
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


@dataclass(frozen=True, slots=True)
class RangeExpansionRealtimeV2Params:
    """Frozen pre-arm contract for the range breakout successor.

    V1 required the expanding candle's body and volume to be complete before
    creating an arm.  That made quote acceptance real-time only *after* the
    move had already been discovered.  V2 deliberately uses only information
    available at the preceding 15-minute close.  The live BBO break supplies
    the expansion event itself.
    """

    minimum_lagged_volume_ratio: float = 0.5

    def __post_init__(self) -> None:
        if self.minimum_lagged_volume_ratio <= 0:
            raise ValueError("pre-arm volume floor must be positive")


RANGE_PREARM_PARAMS: Final = RangeExpansionRealtimeV2Params()


class RangeExpansionRealtimeV2(RangeExpansionRealtimeV1):
    """Pre-arm the prior-range boundary; let live quotes discover the move.

    The scanner is still causal and closed-bar driven for setup state.  It no
    longer waits for the current 15-minute candle to prove expansion, body,
    or current volume.  A meaningful 12-hour boundary, a ready hour profile,
    lagged liquidity, data quality, session timing, and after-cost geometry
    are fixed before the BBO is allowed to start the three-second hold.
    """

    strategy_id = "range_expansion_realtime_v2"
    prearm_params = RANGE_PREARM_PARAMS

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out = super().prepare(candles)
        p = self.params
        prearm = self.prearm_params
        timestamp = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
        # Candle timestamps are open times.  Setup state becomes actionable at
        # the close boundary, including the 11:45 -> 12:00 UTC session handoff.
        decision_at = timestamp + pd.Timedelta(minutes=15)
        close = pd.to_numeric(out["close"], errors="coerce")
        long_level = pd.to_numeric(out["rt_long_level"], errors="coerce")
        short_level = pd.to_numeric(out["rt_short_level"], errors="coerce")
        atr = pd.to_numeric(out["rex3_atr"], errors="coerce")
        volume = pd.to_numeric(out["volume"], errors="coerce")
        volume_base = pd.to_numeric(out["rex3_volume_base"], errors="coerce")
        lagged_volume_ratio = volume.div(volume_base)
        profile = pd.to_numeric(out["rex3_hour_median_bps"], errors="coerce")
        projected = pd.to_numeric(out["rex3_projected_net_bps"], errors="coerce")
        quality_ok = out["rex3_quality_ok"].eq(1)
        session_ok = decision_at.dt.hour.ge(p.session_start_hour_utc) & decision_at.dt.hour.lt(
            p.session_end_hour_utc
        )
        profile_ready = profile.gt(0)
        liquidity_ok = lagged_volume_ratio.ge(prearm.minimum_lagged_volume_ratio)
        geometry_ok = atr.gt(0) & long_level.gt(short_level) & projected.ge(p.min_projected_net_bps)
        common = quality_ok & session_ok & profile_ready & liquidity_ok & geometry_ok
        allow_long = common & close.le(long_level)
        allow_short = common & close.ge(short_level)
        out["rt_decision_at"] = decision_at
        out["rt_prearm_session_ok"] = session_ok.astype(float)
        out["rt_prearm_profile_ready"] = profile_ready.astype(float)
        out["rt_prearm_volume_ratio"] = lagged_volume_ratio
        out["rt_prearm_liquidity_ok"] = liquidity_ok.astype(float)
        out["rt_prearm_geometry_ok"] = geometry_ok.astype(float)
        out["rt_allow_long"] = allow_long.astype(float)
        out["rt_allow_short"] = allow_short.astype(float)
        out["rt_arm_ready"] = (allow_long | allow_short).astype(float)
        return out

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

        quality_ok = flag("rex3_quality_ok")
        session_ok = flag("rt_prearm_session_ok")
        profile_ready = flag("rt_prearm_profile_ready")
        liquidity_ok = flag("rt_prearm_liquidity_ok")
        geometry_ok = flag("rt_prearm_geometry_ok")
        inside_boundary = flag("rt_allow_long") or flag("rt_allow_short")
        failures = [
            reason
            for ok, reason in (
                (quality_ok, "data_quality_not_ok"),
                (session_ok, "fill_session_not_open"),
                (profile_ready, "hour_profile_not_ready"),
                (liquidity_ok, "lagged_liquidity_below_floor"),
                (geometry_ok, "after_cost_geometry_below_floor"),
                (inside_boundary, "boundary_already_crossed"),
            )
            if not ok
        ]
        return {
            "eligible": flag("rt_arm_ready"),
            "primary_failed_gate": failures[0] if failures else None,
            "all_failed_gates": failures,
            "features": {
                "entry_mode": "prearmed_live_quote_hold",
                "setup_bar_requires_expansion": False,
                "setup_bar_requires_current_volume": False,
                "decision_at": str(row.get("rt_decision_at", "unavailable")),
                "prior_range_high": number("rt_long_level"),
                "prior_range_low": number("rt_short_level"),
                "lagged_volume_ratio": number("rt_prearm_volume_ratio"),
                "hour_profile_bps": number("rex3_hour_median_bps"),
                "projected_net_bps": number("rex3_projected_net_bps"),
            },
            "thresholds": {
                "minimum_lagged_volume_ratio": self.prearm_params.minimum_lagged_volume_ratio,
                "min_projected_net_bps": self.params.min_projected_net_bps,
                "acceptance_hold_seconds": self.acceptance_params.acceptance_hold_seconds,
                "min_acceptance_samples": self.acceptance_params.min_acceptance_samples,
                "max_chase_bps": self.acceptance_params.max_chase_bps,
            },
        }


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
        allow_long = bool(row["rt_allow_long"])
        allow_short = bool(row["rt_allow_short"])
        try:
            evidence = self.freeze_permission_snapshot(
                row,
                allow_long=allow_long,
                allow_short=allow_short,
                reason=self.strategy_id,
            )
        except MissingHtfContext:
            return None
        except (TypeError, ValueError):
            return None
        return RealtimeEntryArm(
            episode_id=_episode(row, index),
            bar_index=index,
            long_level=long_level,
            short_level=short_level,
            atr=atr,
            reference_price=close,
            allow_long=allow_long,
            allow_short=allow_short,
            long_structural_stop=float(row["rt_long_structural_stop"]),
            short_structural_stop=float(row["rt_short_structural_stop"]),
            expires_after_bars=1,
            session_start_hour_utc=self.params.session_start_hour_utc,
            session_end_hour_utc=self.params.session_end_hour_utc,
            reason=self.strategy_id,
            evidence=evidence,
        )


class StructureBosRealtimeV2(StructureBosRealtimeV1):
    """Decision-time aligned BoS pre-arm with stable swing identity.

    V1 inherited a session flag calculated from the 15-minute candle's open
    time even though the arm becomes available at its close.  It also used a
    row-based episode, replenishing probe state for the same confirmed swing
    every 15 minutes.  V2 fixes both contracts without changing V1 evidence.
    """

    strategy_id = "structure_bos_realtime_v2"
    # V1/V2/V3 historical closed-bar registrations retain their frozen 17 bps
    # generic-swing research assumption. This realtime successor can execute
    # on Delta, so its PRE-ARM payoff filter uses the most conservative enabled
    # swing venue. The runtime CostGate still applies the exact lane profile.
    params = replace(
        StructureBos15mTriggerV3.params,
        round_trip_cost_bps=max(
            CostModel.for_profile("swing").round_trip_bps(),
            CostModel.for_profile("delta_swing").round_trip_bps(),
        ),
    )

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out = super().prepare(candles)
        p = self.params
        timestamp = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
        decision_at = timestamp + pd.Timedelta(minutes=15)
        session_ok = decision_at.dt.hour.ge(p.session_start_hour_utc) & decision_at.dt.hour.lt(
            p.session_end_hour_utc
        )
        close = pd.to_numeric(out["close"], errors="coerce")
        long_level = pd.to_numeric(out["rt_long_level"], errors="coerce")
        short_level = pd.to_numeric(out["rt_short_level"], errors="coerce")
        trend = out["bos15_structure_trend"].astype(str)
        htf = out["bos15_htf_structure_trend"].astype(str)
        bias = out["bos15_dual_avwap_bias"].astype(str)
        common = (
            out["bos15_structure_ready"].fillna(False).astype(bool)
            & out["bos15_quality_ok"].eq(1)
            & session_ok
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
        out["rt_decision_at"] = decision_at
        out["rt_session_ok"] = session_ok.astype(float)
        out["rt_allow_long"] = allow_long.astype(float)
        out["rt_allow_short"] = allow_short.astype(float)
        out["rt_arm_ready"] = (allow_long | allow_short).astype(float)
        return out

    def realtime_arm(self, df: pd.DataFrame, index: int) -> RealtimeEntryArm | None:
        arm = super().realtime_arm(df, index)
        if arm is None:
            return None
        return RealtimeEntryArm(
            episode_id=_level_episode(arm.long_level, arm.short_level),
            bar_index=arm.bar_index,
            long_level=arm.long_level,
            short_level=arm.short_level,
            atr=arm.atr,
            reference_price=arm.reference_price,
            allow_long=arm.allow_long,
            allow_short=arm.allow_short,
            long_structural_stop=arm.long_structural_stop,
            short_structural_stop=arm.short_structural_stop,
            structural_stop_mode=arm.structural_stop_mode,
            expires_after_bars=arm.expires_after_bars,
            session_start_hour_utc=arm.session_start_hour_utc,
            session_end_hour_utc=arm.session_end_hour_utc,
            reason=self.strategy_id,
            evidence=arm.evidence,
        )

    def evaluation_diagnostics(self, df: pd.DataFrame, index: int) -> dict[str, object]:
        row = df.iloc[index]
        missing_context = self._missing_permission_context(row)

        def number(name: str) -> float | None:
            try:
                value = float(row.get(name, float("nan")))
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) else None

        def flag(name: str) -> bool:
            return bool(number(name) or 0)

        trend = str(row.get("bos15_structure_trend", "unavailable"))
        htf = str(row.get("bos15_htf_structure_trend", "unavailable"))
        bias = str(row.get("bos15_dual_avwap_bias", "unavailable"))
        long_context = trend == "up" and htf == "up"
        short_context = trend == "down" and htf == "down"
        aligned = long_context or short_context
        bias_ok = not (
            (long_context and bias == "strong_short") or (short_context and bias == "strong_long")
        )
        projected = number(
            "bos15_projected_net_long_bps" if long_context else "bos15_projected_net_short_bps"
        )
        edge_ok = projected is not None and projected >= self.params.min_projected_net_bps
        inside = flag("rt_allow_long") or flag("rt_allow_short")
        ready_raw = row.get("bos15_structure_ready", False)
        structure_ready = False if pd.isna(ready_raw) else bool(ready_raw)
        checks = (
            (flag("bos15_quality_ok"), "data_quality_not_ok"),
            (flag("rt_session_ok"), "fill_session_not_open"),
            (structure_ready, "confirmed_swing_pair_not_ready"),
            (flag("bos15_volume_ok"), "setup_volume_below_floor"),
            (aligned, "htf_structure_conflict"),
            (bias_ok, "dual_avwap_conflict"),
            (edge_ok, "payoff_hypothesis_below_threshold"),
            (inside, "boundary_already_crossed"),
        )
        failures = [reason for ok, reason in checks if not ok]
        if missing_context:
            failures.insert(0, "htf_context_missing")
        return {
            "eligible": not missing_context and flag("rt_arm_ready"),
            "primary_failed_gate": failures[0] if failures else None,
            "all_failed_gates": failures,
            "features": {
                "entry_mode": "prearmed_live_quote_hold",
                "decision_at": str(row.get("rt_decision_at", "unavailable")),
                "structure_1h": trend,
                "structure_4h": htf,
                "dual_avwap_bias": bias,
                "projected_net_bps": projected,
                "long_level": number("rt_long_level"),
                "short_level": number("rt_short_level"),
                "htf_context_missing": list(missing_context),
            },
            "thresholds": {
                "min_projected_net_bps": self.params.min_projected_net_bps,
                "acceptance_hold_seconds": self.acceptance_params.acceptance_hold_seconds,
                "min_acceptance_samples": self.acceptance_params.min_acceptance_samples,
                "max_chase_bps": self.acceptance_params.max_chase_bps,
            },
        }


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


class SessionContinuationRealtimeV2(SessionContinuationRealtimeV1):
    """Session breakout pre-arm aligned to the candle's close timestamp."""

    strategy_id = "session_continuation_realtime_v2"

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out = super().prepare(candles)
        p = self.params
        timestamp = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
        decision_at = timestamp + pd.Timedelta(minutes=15)
        session_ok = decision_at.dt.hour.ge(p.start_hour) & decision_at.dt.hour.lt(p.end_hour)
        close = pd.to_numeric(out["close"], errors="coerce")
        high = pd.to_numeric(out["rt_long_level"], errors="coerce")
        low = pd.to_numeric(out["rt_short_level"], errors="coerce")
        common = (
            out["rs_quality_ok"].eq(1)
            & out["rs_route_ok"].eq(1)
            & session_ok
            & out["sc15_volume_ok"].eq(1)
        )
        allow_long = common & close.le(high)
        allow_short = common & close.ge(low)
        out["rt_decision_at"] = decision_at
        out["rt_session_ok"] = session_ok.astype(float)
        out["rt_allow_long"] = allow_long.astype(float)
        out["rt_allow_short"] = allow_short.astype(float)
        out["rt_arm_ready"] = (allow_long | allow_short).astype(float)
        return out

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

        inside = flag("rt_allow_long") or flag("rt_allow_short")
        checks = (
            (flag("rs_quality_ok"), "data_quality_not_ok"),
            (flag("rs_route_ok"), "regime_route_blocked"),
            (flag("rt_session_ok"), "fill_session_not_open"),
            (flag("sc15_volume_ok"), "setup_volume_below_floor"),
            (inside, "boundary_already_crossed"),
        )
        failures = [reason for ok, reason in checks if not ok]
        return {
            "eligible": flag("rt_arm_ready"),
            "primary_failed_gate": failures[0] if failures else None,
            "all_failed_gates": failures,
            "features": {
                "entry_mode": "prearmed_live_quote_hold",
                "decision_at": str(row.get("rt_decision_at", "unavailable")),
                "volume_ratio": number("sc15_volume_ratio"),
                "long_level": number("rt_long_level"),
                "short_level": number("rt_short_level"),
            },
            "thresholds": {
                "session_start_hour": self.params.start_hour,
                "session_end_hour": self.params.end_hour,
                "acceptance_hold_seconds": self.acceptance_params.acceptance_hold_seconds,
                "min_acceptance_samples": self.acceptance_params.min_acceptance_samples,
                "max_chase_bps": self.acceptance_params.max_chase_bps,
            },
        }


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
    canonical_context_timeframes = StructureBos15mTriggerV3.canonical_context_timeframes
    requires_permission_snapshot = True

    def _new_structure_engine(
        self,
        funding: pd.DataFrame | None,
    ) -> StructureBos15mTriggerV3:
        """Construct the frozen exact-data structure engine for this ID."""
        return StructureBos15mTriggerV3(funding)

    def __init__(self, funding: pd.DataFrame | None = None) -> None:
        self.funding = funding
        # Keep one structure engine for the lifetime of the scanner.  The
        # runtime seeds canonical 4h context on the strategy instance before
        # the first evaluation; constructing a new engine in ``prepare`` used
        # to discard that context and made every HTF gate read ``none``.
        self._structure = self._new_structure_engine(funding)

    def bind_canonical_context(self, timeframe: str, candles: pd.DataFrame) -> None:
        self._structure.bind_canonical_context(timeframe, candles)

    def ingest_canonical_context(self, candle: Candle) -> None:
        self._structure.ingest_canonical_context(candle)

    def set_canonical_context_health(self, timeframe: str, healthy: bool) -> None:
        self._structure.set_canonical_context_health(timeframe, healthy)

    def freeze_permission_snapshot(
        self,
        row: pd.Series,
        *,
        allow_long: bool,
        allow_short: bool,
        reason: str,
    ):
        return self._structure.freeze_permission_snapshot(
            row,
            allow_long=allow_long,
            allow_short=allow_short,
            reason=reason,
        )

    def _missing_permission_context(self, row: pd.Series) -> tuple[str, ...]:
        return self._structure._missing_permission_context(row)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        # Build the frozen BoS context with its own parameter object.  This
        # scanner's public ``params`` are the pullback contract below, so the
        # workflow UI never reports the inherited BoS thresholds by mistake.
        out = self._structure.prepare(candles)
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
        allow_long = bool(row["rt_allow_long"])
        allow_short = bool(row["rt_allow_short"])
        try:
            evidence = self.freeze_permission_snapshot(
                row,
                allow_long=allow_long,
                allow_short=allow_short,
                reason=self.strategy_id,
            )
        except MissingHtfContext:
            return None
        except (TypeError, ValueError):
            return None
        return RealtimeEntryArm(
            episode_id=_episode(row, index),
            bar_index=index,
            long_level=long_level,
            short_level=short_level,
            atr=atr,
            reference_price=close,
            allow_long=allow_long,
            allow_short=allow_short,
            long_structural_stop=float(row["rt_long_structural_stop"]),
            short_structural_stop=float(row["rt_short_structural_stop"]),
            structural_stop_mode="structure_floor",
            expires_after_bars=2,
            reason=self.strategy_id,
            evidence=evidence,
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
        missing_context = self._missing_permission_context(row)

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
        if missing_context:
            failures.insert(0, "htf_context_missing")
        eligible = not missing_context and flag("rt_arm_ready")
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
                "htf_context_missing": list(missing_context),
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
    RangeExpansionRealtimeV2,
    StructureBosRealtimeV1,
    StructureBosRealtimeV2,
    SessionContinuationRealtimeV1,
    SessionContinuationRealtimeV2,
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
