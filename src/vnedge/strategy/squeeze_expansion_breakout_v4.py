"""Exact-volume squeeze acceptance features (v4, research/shadow only).

V3 proved the quote-held trigger contract but still armed from a close-volume
VWAP proxy and left its volume-confirmation feature non-binding. V4 is a new
registration so the old evidence remains reproducible: it requires a complete
canonical quote/base-volume window and a confirmed expansion-volume bar before
the runtime may arm the quote acceptance engine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

import pandas as pd  # type: ignore[import-untyped]

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.squeeze_expansion_breakout import PARAMS as FEATURE_PARAMS
from vnedge.strategy.squeeze_expansion_breakout import SqueezeExpansionBreakout
from vnedge.strategy.squeeze_expansion_breakout_v3 import PARAMS as ACCEPTANCE_PARAMS


@dataclass(frozen=True, slots=True)
class SqueezeExpansionV4Params:
    exact_vwap_bars: int = 288
    require_volume_confirmation: bool = True

    def __post_init__(self) -> None:
        if self.exact_vwap_bars < 2:
            raise ValueError("exact_vwap_bars must be at least 2")


PARAMS: Final = SqueezeExpansionV4Params()

STRATEGY_SPEC = MappingProxyType(
    {
        "strategy_id": "squeeze_expansion_breakout_v4",
        "eligibility": "RESEARCH_ONLY",
        "capital_eligible": False,
        "tradeable": False,
        "timeframe": "5m",
        "params": PARAMS,
        "acceptance_params": ACCEPTANCE_PARAMS,
        "purpose": "exact-volume squeeze arms with quote-held breakout acceptance",
    }
)


class SqueezeExpansionBreakoutV4(BaseStrategy):
    strategy_id = "squeeze_expansion_breakout_v4"
    eligibility = "RESEARCH_ONLY"
    timeframe = "5m"
    params = PARAMS
    warmup_bars = max(
        SqueezeExpansionBreakout.warmup_bars,
        PARAMS.exact_vwap_bars + 1,
    )

    def __init__(self, funding: pd.DataFrame | None = None) -> None:
        self.funding = funding
        self._features = SqueezeExpansionBreakout(funding)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out = self._features.prepare(candles)
        required = {"volume", "quote_volume", "trade_count"}
        if not required.issubset(out.columns):
            out["sqz_exact_volume_ready"] = 0.0
            out["sqz_arm_ready"] = 0.0
            out["sqz_vwap_source"] = "unavailable"
            # V3's proxy must never leak into a V4 arm.
            out["sqz_vwap24"] = float("nan")
            out["sqz_compressed_raw"] = out["sqz_compressed"]
            out["sqz_compressed"] = 0.0
            return out

        volume = pd.to_numeric(out["volume"], errors="coerce")
        quote = pd.to_numeric(out["quote_volume"], errors="coerce")
        trades = pd.to_numeric(out["trade_count"], errors="coerce")
        quality = (
            out["data_quality"].astype(str).str.lower().eq("ok")
            if "data_quality" in out.columns
            else pd.Series(False, index=out.index)
        )
        closed = (
            out["is_closed"].eq(True).fillna(False).astype(bool)
            if "is_closed" in out.columns
            else pd.Series(False, index=out.index)
        )
        exact_row = (
            quality
            & closed
            & volume.gt(0)
            & quote.gt(0)
            & trades.gt(0)
        )
        p = self.params
        exact_ready = (
            exact_row.shift(1, fill_value=False)
            .rolling(p.exact_vwap_bars, min_periods=p.exact_vwap_bars)
            .sum()
            .eq(p.exact_vwap_bars)
        )
        base_sum = volume.shift(1).rolling(
            p.exact_vwap_bars, min_periods=p.exact_vwap_bars
        ).sum()
        quote_sum = quote.shift(1).rolling(
            p.exact_vwap_bars, min_periods=p.exact_vwap_bars
        ).sum()
        exact_vwap = quote_sum.div(base_sum.where(base_sum.gt(0))).where(exact_ready)
        volume_ok = pd.to_numeric(out["sqz_volume_ok"], errors="coerce").gt(0)
        compressed = pd.to_numeric(out["sqz_compressed"], errors="coerce").gt(0)
        arm_ready = exact_ready & compressed
        if p.require_volume_confirmation:
            arm_ready &= volume_ok

        out["sqz_compressed_raw"] = out["sqz_compressed"]
        out["sqz_exact_volume_ready"] = exact_ready.astype(float)
        out["sqz_arm_ready"] = arm_ready.astype(float)
        out["sqz_vwap24"] = exact_vwap
        out["sqz_vwap_source"] = exact_ready.map(
            {True: "canonical_quote_over_base", False: "unavailable"}
        )
        # The acceptance runner consumes sqz_compressed as the arm switch.
        out["sqz_compressed"] = arm_ready.astype(float)
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        del df, index
        return None

    def evaluation_diagnostics(self, df: pd.DataFrame, index: int) -> dict[str, object]:
        """Explain the closed-bar arm decision; quote acceptance is separate."""
        row = df.iloc[index]

        def number(name: str) -> float | None:
            try:
                value = float(row.get(name, float("nan")))
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) else None

        exact_ready = bool(number("sqz_exact_volume_ready") or 0)
        compressed = bool(number("sqz_compressed_raw") or 0)
        volume_ok = bool(number("sqz_volume_ok") or 0)
        arm_ready = bool(number("sqz_arm_ready") or 0)
        quality_ok = str(row.get("data_quality", "unknown")).lower() == "ok"
        closed_value = row.get("is_closed", False)
        closed = False if pd.isna(closed_value) else bool(closed_value)
        failures: list[str] = []
        if not quality_ok:
            failures.append("data_quality_not_ok")
        if not closed:
            failures.append("forming_bar")
        if not exact_ready:
            failures.append("exact_volume_window_not_ready")
        if not compressed:
            failures.append("compression_not_present")
        if not volume_ok:
            failures.append("volume_confirmation_failed")

        rank = number("sqz_range_rank")
        volume = number("volume")
        volume_base = number("sqz_vol_ma")
        volume_ratio = (
            volume / volume_base
            if volume is not None and volume_base is not None and volume_base > 0
            else None
        )
        return {
            "eligible": arm_ready,
            "primary_failed_gate": failures[0] if failures else None,
            "all_failed_gates": failures,
            "features": {
                "sqz_exact_volume_ready": exact_ready,
                "sqz_compressed_raw": compressed,
                "sqz_volume_ok": volume_ok,
                "sqz_arm_ready": arm_ready,
                "sqz_range_rank": rank,
                "sqz_volume_ratio": volume_ratio,
                "sqz_range_high": number("sqz_range_high"),
                "sqz_range_low": number("sqz_range_low"),
                "sqz_atr": number("sqz_atr"),
                "sqz_vwap24": number("sqz_vwap24"),
                "sqz_vwap_source": str(row.get("sqz_vwap_source", "unavailable")),
            },
            "thresholds": {
                "exact_vwap_bars": self.params.exact_vwap_bars,
                "compression_threshold": FEATURE_PARAMS.compression_threshold,
                "min_volume_mult": FEATURE_PARAMS.min_volume_mult,
                "acceptance_hold_seconds": ACCEPTANCE_PARAMS.acceptance_hold_seconds,
                "min_acceptance_samples": ACCEPTANCE_PARAMS.min_acceptance_samples,
                "max_chase_bps": ACCEPTANCE_PARAMS.max_chase_bps,
            },
            "distance_to_threshold": {
                "compression_rank_excess": (
                    None
                    if rank is None
                    else max(0.0, rank - FEATURE_PARAMS.compression_threshold)
                ),
                "volume_ratio_shortfall": (
                    None
                    if volume_ratio is None
                    else max(0.0, FEATURE_PARAMS.min_volume_mult - volume_ratio)
                ),
            },
        }


__all__ = [
    "ACCEPTANCE_PARAMS",
    "PARAMS",
    "STRATEGY_SPEC",
    "SqueezeExpansionBreakoutV4",
    "SqueezeExpansionV4Params",
]
