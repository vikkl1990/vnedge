"""Route-neutral structure-bounce cohort for shadow execution research.

The historical ``structure_bounce_prod_v1`` result is burned and remains
untouched.  V2 asks a narrower question: given one frozen sequence of causal
5-minute structure-bounce setups, does crossing immediately or resting at the
structure level produce the better *realised* route on Delta India?

The strategy therefore emits the same stop, target, and passive reference for
both routes.  ``LaneSpec.entry_route`` is the only permitted difference.  A
candidate is released immediately after emission so a fill, miss, win, or loss
in one route cannot alter the other route's future setup eligibility.

RESEARCH_ONLY / SHADOW_OBSERVE.  This module never grants capital permission.
"""

from __future__ import annotations

import math
import statistics
from types import MappingProxyType
from typing import Final

import pandas as pd

from vnedge.execution.trigger_engine import TriggerConfig, TriggerEngine
from vnedge.strategy.arm_sources import Bar, BarContext, StructureBounceArmSource
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.bounce_lanes import SHARED_ARM, SHARED_GATE, SHARED_TRIGGER
from vnedge.strategy.production_filters import ProductionGate

STRATEGY_ID: Final = "structure_bounce_route_probe_v2"
REWARD_R: Final = 2.5
ATR_PERIOD: Final = 48
VOLUME_LOOKBACK: Final = 48
VWAP_BARS: Final = 288

STRATEGY_SPEC = MappingProxyType(
    {
        "strategy_id": STRATEGY_ID,
        "eligibility": "RESEARCH_ONLY",
        "capital_eligible": False,
        "tradeable": False,
        "timeframe": "5m",
        "entry_clock": "configured_route_after_5m_close",
        "setup_cohort": "route_neutral_structure_bounce_v2",
        "target_r": REWARD_R,
        "purpose": "paired Delta shadow execution-route evidence",
    }
)


def _bars(frame: pd.DataFrame) -> list[Bar]:
    ts = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    if ts.isna().any():
        raise ValueError("route probe requires valid UTC timestamps")
    numeric = {
        name: pd.to_numeric(frame[name], errors="coerce")
        for name in ("open", "high", "low", "close", "volume")
    }
    if any(series.isna().any() for series in numeric.values()):
        raise ValueError("route probe requires finite OHLCV")
    return [
        (
            int(stamp.timestamp() * 1_000),
            float(numeric["open"].iloc[index]),
            float(numeric["high"].iloc[index]),
            float(numeric["low"].iloc[index]),
            float(numeric["close"].iloc[index]),
            float(numeric["volume"].iloc[index]),
        )
        for index, stamp in enumerate(ts)
    ]


def _atr(bars: list[Bar], index: int, period: int) -> float:
    if index < period + 1:
        return 0.0
    return statistics.mean(
        max(
            bars[j][2] - bars[j][3],
            abs(bars[j][2] - bars[j - 1][4]),
            abs(bars[j][3] - bars[j - 1][4]),
        )
        for j in range(index - period, index)
    )


class StructureBounceRouteProbeV2(BaseStrategy):
    """One causal setup cohort whose route is supplied by the lane contract."""

    strategy_id = STRATEGY_ID
    eligibility = "RESEARCH_ONLY"
    timeframe = "5m"
    reward_r = REWARD_R
    warmup_bars = StructureBounceArmSource(**SHARED_ARM).warmup_bars

    def __init__(self, funding: pd.DataFrame | None = None) -> None:
        self.funding = funding

    @staticmethod
    def _new_gate() -> ProductionGate:
        return ProductionGate(
            inner=StructureBounceArmSource(**SHARED_ARM),
            **SHARED_GATE,
        )

    @staticmethod
    def _new_trigger() -> TriggerEngine:
        # Setup generation always uses the crossing form.  The downstream
        # execution route chooses BBO taker versus passive structure retest.
        return TriggerEngine(
            config=TriggerConfig(**SHARED_TRIGGER, entry_mode="close")
        )

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = required.difference(candles.columns)
        if missing:
            raise ValueError(f"route probe missing columns: {sorted(missing)}")

        out = candles.copy()
        bars = _bars(out)
        count = len(out)
        fire_side = [""] * count
        fire_level = [float("nan")] * count
        fire_stop = [float("nan")] * count
        confidence = [0] * count
        regime = ["unavailable"] * count
        status = ["warming"] * count
        quality_ok = (
            out["data_quality"].astype(str).str.lower().eq("ok").tolist()
            if "data_quality" in out.columns
            else [True] * count
        )

        gate = self._new_gate()
        trigger = self._new_trigger()
        pv = 0.0
        vv = 0.0
        feature_start = max(ATR_PERIOD, VOLUME_LOOKBACK) + 1

        for index in range(count):
            if index >= 1:
                previous = bars[index - 1]
                pv += previous[4] * previous[5]
                vv += previous[5]
                if index - 1 >= VWAP_BARS:
                    expired = bars[index - 1 - VWAP_BARS]
                    pv -= expired[4] * expired[5]
                    vv -= expired[5]
            if index < feature_start:
                continue

            atr = _atr(bars, index, ATR_PERIOD)
            vol_ma = statistics.mean(
                bar[5] for bar in bars[index - VOLUME_LOOKBACK : index]
            )
            vwap = pv / vv if vv > 0 else None
            ctx = BarContext(
                bars=bars,
                index=index,
                atr=atr,
                vol_ma=vol_ma,
                vwap=vwap,
                prev_close=bars[index - 1][4],
            )
            blocked_before = dict(gate.blocked)
            arm = gate.observe(ctx)
            confidence[index] = int(getattr(gate.inner, "last_confidence", 0))
            snapshot = gate.last_regime
            regime[index] = snapshot.label if snapshot is not None else "unavailable"

            if index < self.warmup_bars:
                status[index] = "warming"
                continue
            if not quality_ok[index]:
                status[index] = "data_quality_not_ok"
                continue
            if arm is None:
                changed = [
                    reason
                    for reason, total in gate.blocked.items()
                    if total > blocked_before.get(reason, 0)
                ]
                status[index] = changed[0] if changed else "no_structure_bounce"
                continue

            fire = trigger.try_fire(
                arm=arm,
                high=bars[index][2],
                low=bars[index][3],
                close=bars[index][4],
                volume=bars[index][5],
                vwap=vwap,
                bar_index=index,
                bar_ts_ms=bars[index][0],
            )
            if fire is None:
                status[index] = (
                    trigger.last_reject.value
                    if trigger.last_reject is not None
                    else "trigger_rejected"
                )
                continue

            close = bars[index][4]
            risk = close - fire.stop if fire.side == "long" else fire.stop - close
            if risk <= 0 or fire.level <= 0:
                status[index] = "invalid_route_geometry"
                trigger.notify_cancelled(index)
                continue
            fire_side[index] = fire.side
            fire_level[index] = fire.level
            fire_stop[index] = fire.stop
            status[index] = "candidate"
            # Critical paired-experiment invariant: outcome A cannot change
            # whether cohort B gets the next setup.
            trigger.notify_cancelled(index)

        out["sbrp_side"] = fire_side
        out["sbrp_level"] = fire_level
        out["sbrp_stop"] = fire_stop
        out["sbrp_confidence"] = confidence
        out["sbrp_regime"] = regime
        out["sbrp_status"] = status
        out["sbrp_candidate"] = pd.Series(
            [side in {"long", "short"} for side in fire_side], index=out.index
        ).astype(float)
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index < self.warmup_bars or index >= len(df):
            return None
        row = df.iloc[index]
        side = str(row.get("sbrp_side", ""))
        if side not in {"long", "short"}:
            return None
        close = float(row["close"])
        stop = float(row["sbrp_stop"])
        level = float(row["sbrp_level"])
        risk = close - stop if side == "long" else stop - close
        if not all(math.isfinite(value) and value > 0 for value in (close, stop, level, risk)):
            return None
        target = close + self.reward_r * risk if side == "long" else close - self.reward_r * risk
        if target <= 0:
            return None
        return SignalIntent(
            side=side,  # type: ignore[arg-type]
            stop_price=stop,
            take_profit_price=target,
            entry_limit_price=level,
            reason=(
                f"{self.strategy_id} side={side} cohort=route_neutral "
                f"confidence={int(row['sbrp_confidence'])} "
                f"regime={row['sbrp_regime']} virtual_only"
            ),
        )

    def evaluation_diagnostics(self, df: pd.DataFrame, index: int) -> dict[str, object]:
        row = df.iloc[index]
        status = str(row.get("sbrp_status", "not_prepared"))
        eligible = status == "candidate"
        return {
            "eligible": eligible,
            "primary_failed_gate": None if eligible else status,
            "all_failed_gates": [] if eligible else [status],
            "features": {
                "setup_cohort": "route_neutral_structure_bounce_v2",
                "passive_level": (
                    float(row["sbrp_level"])
                    if eligible and math.isfinite(float(row["sbrp_level"]))
                    else None
                ),
                "confidence": int(row.get("sbrp_confidence", 0)),
                "regime": str(row.get("sbrp_regime", "unavailable")),
            },
        }


__all__ = ["STRATEGY_ID", "STRATEGY_SPEC", "StructureBounceRouteProbeV2"]
