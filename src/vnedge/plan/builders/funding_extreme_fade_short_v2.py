"""funding_extreme_fade_short_v2 — locked v2 rework onto the TradePlan contract.

The first dead-scanner rework under the Scanner Rework Protocol. A locked,
short-only funding-family hypothesis (NOT a revival of an old file): when
perpetual longs are crowded (funding rich) AND price is stretched above a recent
mean AND we are not in a clean uptrend, fade the stretch short. Params are frozen
in docs/prereg/funding_extreme_fade_short_v2_20260812. Emits ``TradePlan | None``;
never places an order. plan_gate is the sole hard filter.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from vnedge.plan.cost_model import CostModel
from vnedge.plan.trade_plan import (
    AISpec, CostSpec, EntrySpec, ProfitSpec, RiskSpec, Target, TradePlan, plan_gate,
)
from vnedge.strategy.indicators import atr as _atr, rolling_percentile, sma, zscore
from vnedge.strategy.regime import RegimeParams, add_regime_columns, regime_warmup_bars

STRATEGY_ID = "funding_extreme_fade_short_v2"
_FEATURES = ("funding_pct", "close_z", "close_mean", "atr", "regime_trend_up")

# Scanner Rework Protocol outcome. This builder is a RESEARCH ARTIFACT ONLY:
# it FAILED its sealed tail (net −361bps, PF 0.72 on 2025-07→2026-06). Not
# promotable, not wired to any lane. See
# research/archive/funding_extreme_fade_short_v2_FAILED_20260813.md.
SEALED_VERDICT = "FAILED 2026-08-13"


@dataclass(frozen=True)
class FundingExtremeFadeShortV2Params:
    # frozen per §3 of the pre-registration — do NOT tune after lock
    funding_pct_window: int = 240
    funding_extreme: float = 0.90
    z_window: int = 48
    z_entry: float = 2.0
    mean_window: int = 48
    atr_window: int = 24
    stop_atr_mult: float = 1.5
    tp1_floor_bps: float = 40.0
    tp1_cost_mult: float = 2.5          # TP1 = max(floor, mult × round-trip cost)
    time_stop_bars: int = 24
    max_entry_slip_bps: float = 15.0
    entry_timeout_bars: int = 3
    regime: RegimeParams = field(default_factory=RegimeParams)


class FundingExtremeFadeShortV2Builder:
    strategy_id = STRATEGY_ID

    def __init__(self, cost_model: CostModel | None = None,
                 params: FundingExtremeFadeShortV2Params | None = None) -> None:
        self.cost_model = cost_model or CostModel()   # swing world per prereg (17bps rt)
        self.p = params or FundingExtremeFadeShortV2Params()

    @property
    def warmup_bars(self) -> int:
        p = self.p
        return max(p.funding_pct_window, p.z_window, p.mean_window,
                   regime_warmup_bars(p.regime)) + 1

    def prepare(self, candles: pd.DataFrame, funding: pd.DataFrame) -> pd.DataFrame:
        """Causal features: funding_pct (rolling pctile), close_z, close_mean, atr,
        regime flags. Funding is attached with a strictly-backward as-of join."""
        df = add_regime_columns(candles, self.p.regime)
        merged = pd.merge_asof(
            df.sort_values("timestamp"),
            funding[["timestamp", "funding_rate"]].sort_values("timestamp"),
            on="timestamp", direction="backward",
        )
        merged["funding_rate"] = merged["funding_rate"].fillna(0.0)
        merged["funding_pct"] = rolling_percentile(merged["funding_rate"], self.p.funding_pct_window)
        merged["close_z"] = zscore(merged["close"], self.p.z_window)
        merged["close_mean"] = sma(merged["close"], self.p.mean_window)
        merged["atr"] = _atr(merged, self.p.atr_window)
        return merged

    def build_plan(self, df: pd.DataFrame, index: int) -> TradePlan | None:
        """On closed bar ``index`` only. Returns a short TradePlan or None.
        Always runs plan_gate before returning a plan."""
        row = df.iloc[index]
        price = float(row["close"])
        if price <= 0:
            return None

        # --- locked entry conditions (§3) -------------------------------------
        funding_pct = row.get("funding_pct")
        close_z = row.get("close_z")
        mean = row.get("close_mean")
        atr = row.get("atr")
        if any(v is None or (isinstance(v, float) and math.isnan(v))
               for v in (funding_pct, close_z, mean, atr)):
            return None
        if bool(row.get("regime_trend_up", False)):
            return None                                   # not in a clean uptrend
        if float(funding_pct) < self.p.funding_extreme:
            return None                                   # crowded longs
        if float(close_z) < self.p.z_entry:
            return None                                   # stretched above mean
        if float(mean) >= price:
            return None                                   # mean must be BELOW price

        # --- risk / targets in bps (§3, §5) -----------------------------------
        stop_bps = float(atr) * self.p.stop_atr_mult / price * 1e4
        if stop_bps <= 0:
            return None
        rt_cost = self.cost_model.round_trip_bps(include_safety=False)   # fees+slip
        tp1_bps = max(self.p.tp1_floor_bps, self.p.tp1_cost_mult * rt_cost)
        mean_bps = (price - float(mean)) / price * 1e4    # short target = distance down to mean
        if mean_bps < tp1_bps:
            targets = (Target(bps=tp1_bps, size_pct=100.0),)   # collapse to single TP
        else:
            targets = (Target(bps=tp1_bps, size_pct=50.0), Target(bps=mean_bps, size_pct=50.0))

        costs = CostSpec(
            fee_entry_bps=self.cost_model.fee_bps(), fee_exit_bps=self.cost_model.fee_bps(),
            slip_entry_bps=self.cost_model.config.default_slip_entry_bps,
            slip_exit_bps=self.cost_model.config.default_slip_exit_bps,
            funding_bps=0.0,                              # conservative: no funding credit claimed
        )
        expected_net = tp1_bps - (costs.round_trip_bps + self.cost_model.config.safety_buffer_bps)
        plan = TradePlan(
            side="short", decision_tf="1h",
            entry=EntrySpec(type="next_open", max_entry_slip_bps=self.p.max_entry_slip_bps,
                            entry_timeout_bars=self.p.entry_timeout_bars),
            risk=RiskSpec(stop_bps=stop_bps),
            profit=ProfitSpec(targets=targets, time_stop_bars=self.p.time_stop_bars),
            costs=costs,
            ai=AISpec(model_id=None, score=1.0, confidence=1.0, expected_net_bps=expected_net),
            source=self.strategy_id,
            reason=(f"funding_pct={float(funding_pct):.2f} z={float(close_z):.2f} "
                    f"not trend_up mean<{price:.0f}"),
        )
        ok, _reasons = plan_gate(plan, self.cost_model)   # sole hard filter
        return plan if ok else None
