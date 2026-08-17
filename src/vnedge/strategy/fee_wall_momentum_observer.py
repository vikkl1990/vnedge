"""Closed-bar fee-wall momentum observer for virtual outcome research.

The observer answers a narrow question: when a directional move first clears
the configured all-in cost wall, does following that move have enough
subsequent room to survive a pre-registered stop/target plan?  A crossing is
measurement, not alpha.  The strategy is therefore restricted to
``SHADOW_OBSERVE`` and can never receive capital permission.

All calculations are causal and run on closed 5-minute bars.  Runtime feed
continuity, staleness, and clock-skew guards remain authoritative upstream.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent


@dataclass(frozen=True, slots=True)
class FeeWallMomentumParams:
    """Frozen research registration; changes require a new strategy ID."""

    fee_wall_bps: float = 13.0
    horizons: tuple[tuple[str, int], ...] = (
        ("5m", 1),
        ("1h", 12),
        ("4h", 48),
        ("24h", 288),
    )
    atr_period: int = 14
    atr_threshold_mult: float = 1.3
    volume_lookback: int = 20
    min_volume_ratio: float = 0.8
    stop_atr_mult: float = 1.8
    min_stop_bps: float = 25.0
    max_stop_bps: float = 70.0
    reward_r: float = 2.5

    def __post_init__(self) -> None:
        if self.fee_wall_bps <= 0:
            raise ValueError("fee wall must be positive")
        if not self.horizons or any(bars < 1 for _, bars in self.horizons):
            raise ValueError("horizons must contain positive bar counts")
        if self.atr_period < 2 or self.volume_lookback < 2:
            raise ValueError("indicator lookbacks must be at least two bars")
        if self.atr_threshold_mult <= 0 or self.min_volume_ratio < 0:
            raise ValueError("threshold and volume settings are invalid")
        if not 0 < self.min_stop_bps <= self.max_stop_bps:
            raise ValueError("stop bounds are invalid")
        if self.stop_atr_mult <= 0 or self.reward_r <= 0:
            raise ValueError("stop multiplier and reward R must be positive")


PARAMS: Final = FeeWallMomentumParams()

STRATEGY_SPEC = MappingProxyType(
    {
        "strategy_id": "fee_wall_momentum_observer_v1",
        "eligibility": "RESEARCH_ONLY",
        "capital_eligible": False,
        "tradeable": False,
        "timeframe": "5m",
        "params": PARAMS,
        "purpose": "fee-wall crossing measurement with virtual SL/TP outcomes",
    }
)


class FeeWallMomentumObserver(BaseStrategy):
    """Emit one virtual intent when a horizon newly clears its cost wall."""

    strategy_id = "fee_wall_momentum_observer_v1"
    eligibility = "RESEARCH_ONLY"
    timeframe = "5m"
    params = PARAMS
    warmup_bars = max(bars for _, bars in PARAMS.horizons)

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        params: FeeWallMomentumParams | None = None,
    ) -> None:
        selected = params or PARAMS
        if selected != PARAMS:
            raise ValueError(
                "fee_wall_momentum_observer_v1 params are frozen; use a new strategy ID"
            )
        self.funding = funding
        self.params = selected

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        required = {"high", "low", "close", "volume"}
        missing = required.difference(candles.columns)
        if missing:
            raise ValueError(f"fee-wall observer missing candle columns: {sorted(missing)}")

        out = candles.copy()
        close = pd.to_numeric(out["close"], errors="coerce")
        high = pd.to_numeric(out["high"], errors="coerce")
        low = pd.to_numeric(out["low"], errors="coerce")
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
        atr = true_range.rolling(
            self.params.atr_period,
            min_periods=self.params.atr_period,
        ).mean()
        out["fee_wall_atr_bps"] = atr.div(close).mul(10_000)
        prior_volume_median = volume.shift(1).rolling(
            self.params.volume_lookback,
            min_periods=self.params.volume_lookback,
        ).median()
        out["fee_wall_volume_ratio"] = volume.div(prior_volume_median.where(
            prior_volume_median > 0
        ))

        for name, bars in self.params.horizons:
            move = close.div(close.shift(bars)).sub(1).mul(10_000)
            volatility_floor = out["fee_wall_atr_bps"].mul(
                self.params.atr_threshold_mult * math.sqrt(bars)
            )
            out[f"fee_wall_move_{name}"] = move
            out[f"fee_wall_threshold_{name}"] = volatility_floor.clip(
                lower=self.params.fee_wall_bps
            )
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index <= 0 or index >= len(df) or index < self.warmup_bars:
            return None
        row = df.iloc[index]
        previous = df.iloc[index - 1]
        close = float(row["close"])
        atr_bps = float(row["fee_wall_atr_bps"])
        volume_ratio = float(row["fee_wall_volume_ratio"])
        if (
            not math.isfinite(close)
            or close <= 0
            or not math.isfinite(atr_bps)
            or atr_bps <= 0
            or not math.isfinite(volume_ratio)
            or volume_ratio < self.params.min_volume_ratio
        ):
            return None

        previous_active_sides: set[str] = set()
        for previous_horizon, _bars in self.params.horizons:
            previous_move = float(previous[f"fee_wall_move_{previous_horizon}"])
            previous_threshold = float(
                previous[f"fee_wall_threshold_{previous_horizon}"]
            )
            if (
                math.isfinite(previous_move)
                and math.isfinite(previous_threshold)
                and abs(previous_move) >= previous_threshold
            ):
                previous_active_sides.add("long" if previous_move > 0 else "short")

        for horizon, _bars in self.params.horizons:
            move = float(row[f"fee_wall_move_{horizon}"])
            threshold = float(row[f"fee_wall_threshold_{horizon}"])
            if not all(math.isfinite(value) for value in (move, threshold)):
                continue
            side = "long" if move > 0 else "short"
            if abs(move) < threshold or side in previous_active_sides:
                continue

            risk_bps = min(
                self.params.max_stop_bps,
                max(self.params.min_stop_bps, atr_bps * self.params.stop_atr_mult),
            )
            reward_bps = risk_bps * self.params.reward_r
            if side == "long":
                stop = close * (1 - risk_bps / 10_000)
                target = close * (1 + reward_bps / 10_000)
            else:
                stop = close * (1 + risk_bps / 10_000)
                target = close * (1 - reward_bps / 10_000)
            return SignalIntent(
                side=side,
                stop_price=stop,
                take_profit_price=target,
                reason=(
                    f"fee_wall_cross horizon={horizon} move={move:+.1f}bps "
                    f"threshold={threshold:.1f}bps fee_wall={self.params.fee_wall_bps:.1f}bps "
                    f"volume_ratio={volume_ratio:.2f} risk={risk_bps:.1f}bps "
                    f"reward={reward_bps:.1f}bps virtual_only"
                ),
            )
        return None
