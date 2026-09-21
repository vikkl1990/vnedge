"""OHLC-only weekly-regime sibling on canonical Delta candles.

V1 remains frozen on exact trade-lake weekly VWAP.  V2 deliberately changes
the weekly classifier under a new strategy ID so canonical candles without
weekly VWAP can produce causal context without an HLC3 substitute.

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

from vnedge.strategy.continuation_rules import (
    assess_continuation,
    diagnostic_flag,
    setup_masks,
)
from vnedge.strategy.htf_regime_continuation_15m import (
    HtfRegimeContinuation15mV1,
)
from vnedge.strategy.market_regime import DEFAULT_CONFIG, MarketRegimeMachine
from vnedge.strategy.structure_bos_15m_trigger_v2 import structure_health_diagnostics
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
        "side_policy": "both",
        "weekly_classifier": "range_structure_v1",
        "vwap_source": None,
        "structure_source": "canonical_ohlc_price_only_v1",
        "context": "weekly/daily/4h permission plus closed 1h/15m reclaim",
    }
)


class HtfRegimeContinuation15mV2(HtfRegimeContinuation15mV1):
    """Canonical-OHLC regime permission with one non-duplicated telescope."""

    strategy_id = STRATEGY_ID
    permission_context_sources = (
        "canonical_tick_lake",
        "router",
        "exchange_ohlcv_validated",
    )
    market_regime_config = replace(
        DEFAULT_CONFIG,
        weekly_classifier="range_structure_v1",
    )

    def _new_structure_engine(
        self,
        funding: pd.DataFrame | None,
    ) -> StructureBos15mTriggerV3:
        """Use causal OHLC structure when canonical rows lack optional fields.

        This behavior is isolated to V2.  It does not synthesize quote volume
        or AVWAP: those measurements remain unavailable until the Delta trade
        lake exists.  V1 keeps the exact trade-lake contract.
        """
        return StructureBos15mTriggerV3(
            funding,
            allow_price_only_context=True,
        )

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return self._apply_price_only_contract(super().prepare(candles))

    def prepare_latest(self, candles: pd.DataFrame) -> pd.DataFrame:
        """Live-only last-row preparation; full ``prepare`` remains reference."""
        return self._apply_price_only_contract(super().prepare_latest(candles))

    def _apply_price_only_contract(self, out: pd.DataFrame) -> pd.DataFrame:
        """Apply V2's explicit OHLC structure rules to a prepared frame."""
        out["mreg_structure_source"] = "canonical_ohlc_price_only_v1"
        out["mreg_avwap_source"] = "unavailable"
        long_ready, short_ready = setup_masks(out, self.params)
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
            return diagnostic_flag(row, name)

        missing_context = self._missing_permission_context(row)
        health = structure_health_diagnostics(row)
        assessment = assess_continuation(
            row,
            self.params,
            context_missing=bool(missing_context),
            parent_ineligible=(
                health.get("bos15_structure_health_reason")
                == "structure_parent_ineligible"
            ),
        )
        features = dict(diagnostics.get("features", {}))
        features.update(
            {
                **health,
                "side_policy": "both",
                "permission_long": flag("mreg_allow_long"),
                "permission_short": flag("mreg_allow_short"),
                "setup_long": flag("rt_allow_long"),
                "setup_short": flag("rt_allow_short"),
                "structure_source": "canonical_ohlc_price_only_v1",
                "avwap_source": "unavailable",
                "entry_structure": "closed_1h_structure_plus_15m_reclaim",
                "htf_context_missing": list(missing_context),
            }
        )
        diagnostics["features"] = features
        diagnostics["eligible"] = assessment.eligible
        diagnostics["all_failed_gates"] = list(assessment.failed_gates)
        diagnostics["primary_failed_gate"] = assessment.primary_failed_gate
        return diagnostics

    def _new_regime_machine(self) -> MarketRegimeMachine:
        return MarketRegimeMachine(self.market_regime_config)


__all__ = ["STRATEGY_ID", "STRATEGY_SPEC", "HtfRegimeContinuation15mV2"]
