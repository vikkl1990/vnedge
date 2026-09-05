"""1h/4h structure with a causal 15-minute break trigger (shadow only).

The original S1 deliberately waited for the 1h close.  This new registration
keeps its confirmed 1h swings and fully closed 4h direction, but carries that
context forward to closed 15-minute children.  It can therefore confirm a
break up to 45 minutes earlier without treating an unconfirmed tick as market
structure.  Entries remain virtual and still pass the shared risk/cost path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

import pandas as pd

from vnedge.data.symbols import canonical_symbol
from vnedge.plan.cost_model import CostModel
from vnedge.strategy.arm_evidence import (
    FrozenPermissionSnapshot,
    MissingHtfContext,
    freeze_permission_from_bound_frames,
    missing_bound_context,
)
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.structure_bos_1h import PARAMS as BOS_1H_PARAMS
from vnedge.strategy.structure_bos_1h import StructureBos1H


@dataclass(frozen=True, slots=True)
class StructureBos15mParams:
    break_buffer_bps: float = float(BOS_1H_PARAMS.break_buffer_bps)
    stop_buffer_bps: float = float(BOS_1H_PARAMS.stop_buffer_bps)
    atr_stop_mult: float = float(BOS_1H_PARAMS.atr_stop_mult)
    reward_r: float = float(BOS_1H_PARAMS.cost_edge_reward_r)
    round_trip_cost_bps: float = CostModel.for_profile("swing").round_trip_bps()
    min_projected_net_bps: float = 4.0
    volume_lookback: int = 96
    volume_mult: float = 1.1
    min_bars_between_signals: int = 48
    session_start_hour_utc: int = 12
    session_end_hour_utc: int = 16

    def __post_init__(self) -> None:
        if self.volume_lookback < 2 or self.volume_mult <= 0:
            raise ValueError("volume settings are invalid")
        if self.reward_r <= 0 or self.atr_stop_mult <= 0:
            raise ValueError("risk settings are invalid")
        if not 0 <= self.session_start_hour_utc < self.session_end_hour_utc <= 24:
            raise ValueError("UTC session settings are invalid")


PARAMS: Final = StructureBos15mParams()
STRATEGY_SPEC = MappingProxyType(
    {
        "strategy_id": "structure_bos_15m_trigger_v2",
        "eligibility": "RESEARCH_ONLY",
        "capital_eligible": False,
        "tradeable": False,
        "timeframe": "15m",
        "params": PARAMS,
        "context": "confirmed 1h swings plus fully closed 4h structure",
    }
)


def _complete_hour_frame(candles: pd.DataFrame) -> pd.DataFrame:
    ts = pd.to_datetime(candles["timestamp"], utc=True, errors="coerce")
    work = candles.copy()
    work["timestamp"] = ts
    work["hour_open"] = ts.dt.floor("h")
    work["minute"] = ts.dt.minute
    if "symbol" in work.columns:
        identities = {
            canonical_symbol(str(value))
            for value in work["symbol"].dropna()
            if str(value).strip()
        }
        if len(identities) > 1:
            raise ValueError(
                f"structure BoS 15m contains multiple symbols: {sorted(identities)}"
            )
        output_symbol = next(iter(identities), "BTCUSDT")
    else:
        output_symbol = "BTCUSDT"
    numeric = ("open", "high", "low", "close", "volume")
    for name in numeric:
        work[name] = pd.to_numeric(work[name], errors="coerce")
    rows: list[dict[str, object]] = []
    for hour_open, group in work.groupby("hour_open", sort=True):
        if set(group["minute"].astype(int)) != {0, 15, 30, 45} or len(group) != 4:
            continue
        quality = (
            group["data_quality"].astype(str).str.lower().eq("ok").all()
            if "data_quality" in group.columns
            else True
        )
        rows.append(
            {
                "timestamp": hour_open,
                "symbol": output_symbol,
                "timeframe": "1h",
                "open": float(group.iloc[0]["open"]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group.iloc[-1]["close"]),
                "volume": float(group["volume"].sum()),
                "quote_volume": float(
                    pd.to_numeric(
                        group.get("quote_volume", pd.Series(0.0, index=group.index)),
                        errors="coerce",
                    ).fillna(0).sum()
                ),
                "trade_count": int(
                    pd.to_numeric(
                        group.get("trade_count", pd.Series(0, index=group.index)),
                        errors="coerce",
                    ).fillna(0).sum()
                ),
                "data_quality": "ok" if quality else "gap",
                "is_closed": True,
            }
        )
    return pd.DataFrame(rows)


class StructureBos15mTriggerV2(BaseStrategy):
    strategy_id = "structure_bos_15m_trigger_v2"
    eligibility = "RESEARCH_ONLY"
    timeframe = "15m"
    params = PARAMS
    # 50 complete 1h bars plus room for 4h context and one trigger child.
    warmup_bars = 224
    canonical_context_timeframes = ("4h",)
    requires_permission_snapshot = True

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        allow_price_only_context: bool = False,
    ) -> None:
        self.funding = funding
        self.allow_price_only_context = bool(allow_price_only_context)
        self._hourly = StructureBos1H(
            funding,
            allow_price_only_live=self.allow_price_only_context,
        )

    def bind_canonical_context(self, timeframe: str, candles: pd.DataFrame) -> None:
        self._hourly.bind_canonical_context(timeframe, candles)

    def ingest_canonical_context(self, candle) -> None:
        self._hourly.ingest_canonical_context(candle)

    def set_canonical_context_health(self, timeframe: str, healthy: bool) -> None:
        self._hourly.set_canonical_context_health(timeframe, healthy)

    def freeze_permission_snapshot(
        self,
        row: pd.Series,
        *,
        allow_long: bool,
        allow_short: bool,
        reason: str,
    ) -> FrozenPermissionSnapshot:
        """Freeze the actual last eligible 4h row, never a calendar guess."""
        return freeze_permission_from_bound_frames(
            row.to_dict(),
            decision_timeframe="15m",
            context_frames={"4h": self._hourly.htf_candles},
            context_health={
                "4h": self._hourly._canonical_htf_current is True,
            },
            required_context=self.canonical_context_timeframes,
            allow_long=allow_long,
            allow_short=allow_short,
            reason=reason,
            regime_version=self.strategy_id,
        )

    def _missing_permission_context(self, row: pd.Series) -> tuple[str, ...]:
        decision_close = (
            pd.Timestamp(row["timestamp"]) + pd.Timedelta(minutes=15)
        ).to_pydatetime()
        return missing_bound_context(
            context_frames={"4h": self._hourly.htf_candles},
            context_health={
                "4h": self._hourly._canonical_htf_current is True,
            },
            required_context=self.canonical_context_timeframes,
            decision_close=decision_close,
        )

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = required.difference(candles.columns)
        if missing:
            raise ValueError(f"structure BoS 15m missing columns: {sorted(missing)}")
        out = candles.copy().reset_index(drop=True)
        ts = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
        if ts.isna().any():
            raise ValueError("structure BoS 15m requires valid UTC timestamps")
        out["timestamp"] = ts
        close = pd.to_numeric(out["close"], errors="coerce")
        volume = pd.to_numeric(out["volume"], errors="coerce")
        volume_base = volume.shift(1).rolling(
            self.params.volume_lookback, min_periods=self.params.volume_lookback
        ).median()

        hours = _complete_hour_frame(out)
        context_columns = (
            "structure_ready",
            "structure_trend",
            "last_swing_high",
            "last_swing_low",
            "dual_avwap_bias",
            "bos_atr",
            "htf_structure_trend",
            "mtf_reason",
        )
        for name in context_columns:
            out[f"bos15_{name}"] = math.nan if name not in {
                "structure_trend", "dual_avwap_bias", "htf_structure_trend", "mtf_reason"
            } else "unavailable"

        if not hours.empty:
            prepared = self._hourly.prepare(hours).copy()
            prepared["available_at"] = pd.to_datetime(
                prepared["timestamp"], utc=True
            ) + pd.Timedelta(hours=1)
            right = prepared[["available_at", *context_columns]].rename(
                columns={name: f"bos15_{name}" for name in context_columns}
            )
            left = pd.DataFrame(
                {"_row": out.index, "decision_at": ts + pd.Timedelta(minutes=15)}
            )
            merged = pd.merge_asof(
                left.sort_values("decision_at"),
                right.sort_values("available_at"),
                left_on="decision_at",
                right_on="available_at",
                direction="backward",
            ).sort_values("_row")
            for name in context_columns:
                out[f"bos15_{name}"] = merged[f"bos15_{name}"].to_numpy()

        p = self.params
        high_level = pd.to_numeric(out["bos15_last_swing_high"], errors="coerce").mul(
            1 + p.break_buffer_bps / 10_000
        )
        low_level = pd.to_numeric(out["bos15_last_swing_low"], errors="coerce").mul(
            1 - p.break_buffer_bps / 10_000
        )
        previous_close = close.shift(1)
        volume_ok = volume.ge(volume_base.mul(p.volume_mult))
        session_ok = ts.dt.hour.ge(p.session_start_hour_utc) & ts.dt.hour.lt(
            p.session_end_hour_utc
        )
        quality_ok = (
            out["data_quality"].astype(str).str.lower().eq("ok")
            if "data_quality" in out.columns
            else pd.Series(True, index=out.index)
        )
        trend = out["bos15_structure_trend"].astype(str)
        htf_trend = out["bos15_htf_structure_trend"].astype(str)
        bias = out["bos15_dual_avwap_bias"].astype(str)
        ready = out["bos15_structure_ready"].fillna(False).astype(bool)
        common = ready & volume_ok & session_ok & quality_ok
        long_raw = (
            common
            & trend.eq("up")
            & htf_trend.eq("up")
            & ~bias.eq("strong_short")
            & previous_close.le(high_level)
            & close.gt(high_level)
        )
        short_raw = (
            common
            & trend.eq("down")
            & htf_trend.eq("down")
            & ~bias.eq("strong_long")
            & previous_close.ge(low_level)
            & close.lt(low_level)
        )
        break_long = previous_close.le(high_level) & close.gt(high_level)
        break_short = previous_close.ge(low_level) & close.lt(low_level)
        fire_long = [False] * len(out)
        fire_short = [False] * len(out)
        spacing_ok = [False] * len(out)
        last_fire = -(10**9)
        for index, (is_long, is_short) in enumerate(
            zip(long_raw.fillna(False), short_raw.fillna(False), strict=True)
        ):
            spacing_ok[index] = index - last_fire >= p.min_bars_between_signals
            if not spacing_ok[index]:
                continue
            if is_long:
                fire_long[index] = True
                last_fire = index
            elif is_short:
                fire_short[index] = True
                last_fire = index
        out["bos15_volume_base"] = volume_base
        out["bos15_volume_ok"] = volume_ok.astype(float)
        out["bos15_session_ok"] = session_ok.astype(float)
        out["bos15_quality_ok"] = quality_ok.astype(float)
        out["bos15_break_long"] = break_long.astype(float)
        out["bos15_break_short"] = break_short.astype(float)
        out["bos15_spacing_ok"] = pd.Series(spacing_ok, index=out.index).astype(float)
        atr = pd.to_numeric(out["bos15_bos_atr"], errors="coerce")
        last_high = pd.to_numeric(out["bos15_last_swing_high"], errors="coerce")
        last_low = pd.to_numeric(out["bos15_last_swing_low"], errors="coerce")
        long_swing_stop = last_low.mul(1 - p.stop_buffer_bps / 10_000)
        short_swing_stop = last_high.mul(1 + p.stop_buffer_bps / 10_000)
        long_stop = pd.concat(
            [long_swing_stop, close - p.atr_stop_mult * atr], axis=1
        ).max(axis=1, skipna=False)
        short_stop = pd.concat(
            [short_swing_stop, close + p.atr_stop_mult * atr], axis=1
        ).min(axis=1, skipna=False)
        out["bos15_projected_net_long_bps"] = (
            (close - long_stop) / close * 10_000 * p.reward_r
            - p.round_trip_cost_bps
        )
        out["bos15_projected_net_short_bps"] = (
            (short_stop - close) / close * 10_000 * p.reward_r
            - p.round_trip_cost_bps
        )
        out["bos15_fire_long"] = pd.Series(fire_long, index=out.index).astype(float)
        out["bos15_fire_short"] = pd.Series(fire_short, index=out.index).astype(float)
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index < self.warmup_bars or index >= len(df):
            return None
        row = df.iloc[index]
        is_long = float(row["bos15_fire_long"]) > 0
        is_short = float(row["bos15_fire_short"]) > 0
        if not (is_long or is_short):
            return None
        close = float(row["close"])
        atr = float(row["bos15_bos_atr"])
        last_high = float(row["bos15_last_swing_high"])
        last_low = float(row["bos15_last_swing_low"])
        if not all(math.isfinite(value) and value > 0 for value in (close, atr, last_high, last_low)):
            return None
        side: Literal["long", "short"] = "long" if is_long else "short"
        if side == "long":
            swing_stop = last_low * (1 - self.params.stop_buffer_bps / 10_000)
            stop = max(swing_stop, close - self.params.atr_stop_mult * atr)
            risk = close - stop
            target = close + self.params.reward_r * risk
        else:
            swing_stop = last_high * (1 + self.params.stop_buffer_bps / 10_000)
            stop = min(swing_stop, close + self.params.atr_stop_mult * atr)
            risk = stop - close
            target = close - self.params.reward_r * risk
        if risk <= 0 or stop <= 0 or target <= 0:
            return None
        projected_net = risk / close * 10_000 * self.params.reward_r - self.params.round_trip_cost_bps
        if projected_net < self.params.min_projected_net_bps:
            return None
        try:
            permission_snapshot = self.freeze_permission_snapshot(
                row,
                allow_long=side == "long",
                allow_short=side == "short",
                reason=self.strategy_id,
            )
        except MissingHtfContext:
            return None
        except (TypeError, ValueError):
            # A context-aware signal without the exact bar that granted its
            # permission is not replayable and must never enter the kernel.
            return None
        return SignalIntent(
            side=side,
            stop_price=stop,
            take_profit_price=target,
            reason=(
                f"structure_bos_15m_trigger_v2 side={side} context=closed_1h_4h "
                f"confirmation=15m projected_net={projected_net:.1f}bps virtual_only"
            ),
            permission_snapshot=permission_snapshot,
        )

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

        p = self.params
        missing_context = self._missing_permission_context(row)
        quality_ok = flag("bos15_quality_ok")
        session_ok = flag("bos15_session_ok")
        ready_raw = row.get("bos15_structure_ready", False)
        structure_ready = bool(ready_raw) if not pd.isna(ready_raw) else False
        volume_ok = flag("bos15_volume_ok")
        trend = str(row.get("bos15_structure_trend", "unavailable"))
        htf_trend = str(row.get("bos15_htf_structure_trend", "unavailable"))
        bias = str(row.get("bos15_dual_avwap_bias", "unavailable"))
        long_context = trend == "up" and htf_trend == "up"
        short_context = trend == "down" and htf_trend == "down"
        alignment_ok = long_context or short_context
        bias_ok = not (
            (long_context and bias == "strong_short")
            or (short_context and bias == "strong_long")
        )
        break_long = flag("bos15_break_long")
        break_short = flag("bos15_break_short")
        break_ok = (long_context and break_long) or (short_context and break_short)
        spacing = flag("bos15_spacing_ok")
        projected_long = number("bos15_projected_net_long_bps")
        projected_short = number("bos15_projected_net_short_bps")
        projected = projected_long if long_context else projected_short if short_context else None
        edge_ok = projected is not None and projected >= p.min_projected_net_bps
        failures: list[str] = []
        if missing_context:
            failures.append("htf_context_missing")
        for ok, reason in (
            (quality_ok, "data_quality_not_ok"),
            (session_ok, "session_closed"),
            (structure_ready, "confirmed_swing_pair_not_ready"),
            (volume_ok, "volume_confirmation_failed"),
            (alignment_ok, "htf_structure_conflict"),
            (bias_ok, "dual_avwap_conflict"),
            (break_ok, "confirmed_swing_not_broken"),
            (spacing, "signal_spacing"),
            (edge_ok, "projected_net_below_threshold"),
        ):
            if not ok:
                failures.append(reason)
        volume = number("volume")
        volume_base = number("bos15_volume_base")
        volume_ratio = (
            volume / volume_base
            if volume is not None and volume_base is not None and volume_base > 0
            else None
        )
        eligible = (
            not missing_context
            and (flag("bos15_fire_long") or flag("bos15_fire_short"))
            and edge_ok
        )
        return {
            "eligible": eligible,
            "primary_failed_gate": failures[0] if failures else None,
            "all_failed_gates": failures,
            "features": {
                "bos15_quality_ok": quality_ok,
                "bos15_session_ok": session_ok,
                "bos15_structure_ready": structure_ready,
                "bos15_structure_trend": trend,
                "bos15_htf_structure_trend": htf_trend,
                "bos15_mtf_reason": str(row.get("bos15_mtf_reason", "unavailable")),
                "bos15_dual_avwap_bias": bias,
                "bos15_volume_ratio": volume_ratio,
                "bos15_volume_ok": volume_ok,
                "bos15_break_long": break_long,
                "bos15_break_short": break_short,
                "bos15_spacing_ok": spacing,
                "bos15_last_swing_high": number("bos15_last_swing_high"),
                "bos15_last_swing_low": number("bos15_last_swing_low"),
                "bos15_projected_net_bps": projected,
                "htf_context_missing": list(missing_context),
            },
            "thresholds": {
                "session_start_hour_utc": p.session_start_hour_utc,
                "session_end_hour_utc": p.session_end_hour_utc,
                "volume_mult": p.volume_mult,
                "break_buffer_bps": p.break_buffer_bps,
                "stop_buffer_bps": p.stop_buffer_bps,
                "reward_r": p.reward_r,
                "min_bars_between_signals": p.min_bars_between_signals,
                "min_projected_net_bps": p.min_projected_net_bps,
                "round_trip_cost_bps": p.round_trip_cost_bps,
            },
            "distance_to_threshold": {
                "volume_ratio_shortfall": (
                    None if volume_ratio is None else max(0.0, p.volume_mult - volume_ratio)
                ),
                "projected_net_bps_shortfall": (
                    None if projected is None else max(0.0, p.min_projected_net_bps - projected)
                ),
            },
        }


__all__ = ["PARAMS", "STRATEGY_SPEC", "StructureBos15mParams", "StructureBos15mTriggerV2"]
