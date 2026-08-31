"""OHLC-only weekly-regime sibling for official Delta candle history.

V1 remains frozen on exact trade-lake weekly VWAP.  V2 deliberately changes
the weekly classifier under a new strategy ID so official candles without
quote volume can produce causal context without an HLC3 substitute.

MarketRegime owns the weekly/daily/4h permission.  Entry timing uses the
closed 1h structure derived from complete 15m children plus a closed 15m
reclaim.  V2 intentionally does not apply the inherited second 4h-BoS veto;
that duplicate telescope produced mutually exclusive permissions.
"""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType
from typing import Final

import pandas as pd

from vnedge.strategy.htf_regime_continuation_15m import (
    HtfRegimeContinuation15mV1,
)
from vnedge.strategy.market_regime import DEFAULT_CONFIG, MarketRegimeMachine
from vnedge.strategy.structure_bos_15m_trigger_v3 import StructureBos15mTriggerV3

STRATEGY_ID: Final = "htf_regime_continuation_15m_v2"
STRATEGY_SPEC = MappingProxyType(
    {
        "strategy_id": STRATEGY_ID,
        "eligibility": "RESEARCH_ONLY",
        "capital_eligible": False,
        "tradeable": False,
        "timeframe": "15m",
        "context_timeframes": ("4h", "1d"),
        "entry_clock": "next_15m_open",
        "weekly_classifier": "range_structure_v1",
        "vwap_source": None,
        "structure_source": "official_ohlc_price_only_v1",
        "context": "weekly/daily/4h permission plus closed 1h/15m reclaim",
    }
)


class HtfRegimeContinuation15mV2(HtfRegimeContinuation15mV1):
    """Official-OHLC regime permission with one non-duplicated telescope."""

    strategy_id = STRATEGY_ID
    market_regime_config = replace(
        DEFAULT_CONFIG,
        weekly_classifier="range_structure_v1",
    )

    def _new_structure_engine(
        self,
        funding: pd.DataFrame | None,
    ) -> StructureBos15mTriggerV3:
        """Use causal OHLC structure when official candles lack trade fields.

        This behavior is isolated to V2.  It does not synthesize quote volume
        or AVWAP: those measurements remain unavailable until the Delta trade
        lake exists.  V1 keeps the exact trade-lake contract.
        """
        return StructureBos15mTriggerV3(
            funding,
            allow_price_only_context=True,
        )

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out = super().prepare(candles)
        out["mreg_structure_source"] = "official_ohlc_price_only_v1"
        out["mreg_avwap_source"] = "unavailable"
        p = self.params
        ready = out["bos15_structure_ready"].fillna(False).astype(bool)
        quality = out["bos15_quality_ok"].eq(1)
        trend = out["bos15_structure_trend"].astype(str)
        bias = out["bos15_dual_avwap_bias"].astype(str)
        meaningful = (
            pd.to_numeric(out["hsc_volume_ratio"], errors="coerce").ge(p.volume_mult)
            & pd.to_numeric(out["hsc_body_bps"], errors="coerce").ge(
                p.min_reclaim_body_bps
            )
        )
        long_ready = (
            ready
            & quality
            & trend.eq("up")
            & ~bias.eq("strong_short")
            & meaningful
            & out["hsc_pullback_long"].eq(1)
            & pd.to_numeric(
                out["hsc_projected_net_long_bps"], errors="coerce"
            ).ge(p.min_projected_net_bps)
            & out["mreg_allow_long"].eq(1)
        )
        short_ready = (
            ready
            & quality
            & trend.eq("down")
            & ~bias.eq("strong_long")
            & meaningful
            & out["hsc_pullback_short"].eq(1)
            & pd.to_numeric(
                out["hsc_projected_net_short_bps"], errors="coerce"
            ).ge(p.min_projected_net_bps)
            & out["mreg_allow_short"].eq(1)
        )
        out["rt_allow_long"] = long_ready.astype(float)
        out["rt_allow_short"] = short_ready.astype(float)
        out["rt_arm_ready"] = (long_ready | short_ready).astype(float)
        return out

    def evaluation_diagnostics(
        self,
        df: pd.DataFrame,
        index: int,
    ) -> dict[str, object]:
        diagnostics = super().evaluation_diagnostics(df, index)
        row = df.iloc[index]

        def flag(name: str) -> bool:
            try:
                return bool(float(row.get(name, 0)))
            except (TypeError, ValueError):
                return False

        side_ready = flag("rt_allow_long") or flag("rt_allow_short")
        regime_side = flag("mreg_allow_long") or flag("mreg_allow_short")
        structure_side = str(row.get("bos15_structure_trend")) in {"up", "down"}
        meaningful = (
            float(row.get("hsc_volume_ratio", 0)) >= self.params.volume_mult
            and float(row.get("hsc_body_bps", 0)) >= self.params.min_reclaim_body_bps
        )
        pullback = flag("hsc_pullback_long") or flag("hsc_pullback_short")
        checks = (
            (flag("mreg_ready"), "market_regime_not_ready"),
            (str(row.get("mreg_state")) == "continuation", "regime_flat"),
            (regime_side, "family_mismatch"),
            (flag("bos15_structure_ready"), "structure_not_ready"),
            (flag("bos15_quality_ok"), "data_quality_not_ok"),
            (structure_side, "one_hour_structure_unresolved"),
            (meaningful, "reclaim_not_meaningful"),
            (pullback, "closed_15m_reclaim_missing"),
            (side_ready, "side_alignment_missing"),
        )
        failures = [reason for ok, reason in checks if not ok]
        features = dict(diagnostics.get("features", {}))
        features.update(
            {
                "structure_source": "official_ohlc_price_only_v1",
                "avwap_source": "unavailable",
                "entry_structure": "closed_1h_structure_plus_15m_reclaim",
            }
        )
        diagnostics["features"] = features
        diagnostics["eligible"] = side_ready
        diagnostics["all_failed_gates"] = failures
        diagnostics["primary_failed_gate"] = failures[0] if failures else None
        return diagnostics

    def _new_regime_machine(self) -> MarketRegimeMachine:
        return MarketRegimeMachine(self.market_regime_config)


__all__ = ["STRATEGY_ID", "STRATEGY_SPEC", "HtfRegimeContinuation15mV2"]
