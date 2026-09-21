"""Pure price-only continuation rules, without feeds, clocks or execution.

This is the frozen V2 contract, not a new strategy. Preparation uses strict
``eq(1)`` flags; legacy diagnostics use truthy numeric flags. Keep that
distinction explicit rather than changing admission during a refactor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pandas as pd


class ContinuationThresholds(Protocol):
    """Only the existing thresholds consumed by this rules layer."""

    @property
    def volume_mult(self) -> float: ...

    @property
    def min_reclaim_body_bps(self) -> float: ...

    @property
    def min_projected_net_bps(self) -> float: ...


def setup_masks(
    frame: pd.DataFrame, thresholds: ContinuationThresholds,
) -> tuple[pd.Series, pd.Series]:
    """Return aligned long/short masks; never mutate the prepared frame."""
    common = (
        frame["bos15_structure_ready"].fillna(False).astype(bool)
        & frame["bos15_quality_ok"].eq(1)
        & pd.to_numeric(frame["hsc_volume_ratio"], errors="coerce").ge(
            thresholds.volume_mult
        )
        & pd.to_numeric(frame["hsc_body_bps"], errors="coerce").ge(
            thresholds.min_reclaim_body_bps
        )
    )
    trend = frame["bos15_structure_trend"].astype(str)

    def side_mask(side: str, direction: str) -> pd.Series:
        return (
            common
            & trend.eq(direction)
            & frame[f"hsc_pullback_{side}"].eq(1)
            & pd.to_numeric(
                frame[f"hsc_projected_net_{side}_bps"], errors="coerce"
            ).ge(thresholds.min_projected_net_bps)
            & frame[f"mreg_allow_{side}"].eq(1)
        )

    return side_mask("long", "up"), side_mask("short", "down")


def diagnostic_flag(row: pd.Series, name: str) -> bool:
    """Legacy diagnostic coercion; not an admission predicate."""
    try:
        value = row.get(name, 0)
        return not pd.isna(value) and bool(float(value))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class ContinuationAssessment:
    eligible: bool
    failed_gates: tuple[str, ...]

    @property
    def primary_failed_gate(self) -> str | None:
        return self.failed_gates[0] if self.failed_gates else None


def assess_continuation(
    row: pd.Series,
    thresholds: ContinuationThresholds,
    *,
    context_missing: bool,
    parent_ineligible: bool,
) -> ContinuationAssessment:
    """Explain a prepared row in stable gate order, with all failures.

    Eligibility deliberately retains V2's setup/context meaning. It is not
    gateway approval, nor a promise that every diagnostic check passed.
    Context identity and structure-health interpretation belong to the caller.
    """
    def flag(name: str) -> bool:
        return diagnostic_flag(row, name)

    side_ready = flag("rt_allow_long") or flag("rt_allow_short")
    meaningful = (
        float(row.get("hsc_volume_ratio", 0)) >= thresholds.volume_mult
        and float(row.get("hsc_body_bps", 0)) >= thresholds.min_reclaim_body_bps
    )
    checks = (
        (not context_missing, "htf_context_missing"),
        (flag("bos15_parent_identity_ok"), "structure_parent_missing"),
        (not parent_ineligible, "gap_parent"),
        (flag("mreg_ready"), "market_regime_not_ready"),
        (str(row.get("mreg_state")) == "continuation", "regime_flat"),
        (flag("mreg_allow_long") or flag("mreg_allow_short"), "family_mismatch"),
        (flag("bos15_structure_ready"), "structure_not_ready"),
        (flag("bos15_quality_ok"), "data_quality_not_ok"),
        (str(row.get("bos15_structure_trend")) in {"up", "down"},
         "one_hour_structure_unresolved"),
        (meaningful, "reclaim_not_meaningful"),
        (flag("hsc_pullback_long") or flag("hsc_pullback_short"),
         "closed_15m_reclaim_missing"),
        (side_ready, "side_alignment_missing"),
    )
    return ContinuationAssessment(
        eligible=side_ready and not context_missing,
        failed_gates=tuple(reason for ok, reason in checks if not ok),
    )
