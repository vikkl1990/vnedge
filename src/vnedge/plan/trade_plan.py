"""The TradePlan contract + bps helpers + the hard cost gate."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from vnedge.plan.cost_model import CostModel

Side = Literal["long", "short"]


def bps_frac(bps: float) -> float:
    return bps / 10_000.0


def target_price(entry: float, bps: float, side: Side) -> float:
    """Price `bps` in the FAVORABLE direction from entry (a take-profit level)."""
    return entry * (1 + bps_frac(bps)) if side == "long" else entry * (1 - bps_frac(bps))


def stop_price(entry: float, bps: float, side: Side) -> float:
    """Price `bps` in the ADVERSE direction from entry (a stop level)."""
    return entry * (1 - bps_frac(bps)) if side == "long" else entry * (1 + bps_frac(bps))


@dataclass(frozen=True)
class EntrySpec:
    type: Literal["next_open", "limit_bps", "ltf_confirm"] = "next_open"
    limit_offset_bps: float | None = None
    max_entry_slip_bps: float = 5.0
    entry_timeout_bars: int = 3


@dataclass(frozen=True)
class RiskSpec:
    stop_bps: float
    invalidation_price: float | None = None


@dataclass(frozen=True)
class Target:
    bps: float
    size_pct: float


@dataclass(frozen=True)
class ProfitSpec:
    targets: tuple[Target, ...]
    time_stop_bars: int
    trail_bps: float | None = None


@dataclass(frozen=True)
class CostSpec:
    fee_entry_bps: float
    fee_exit_bps: float
    slip_entry_bps: float
    slip_exit_bps: float
    funding_bps: float = 0.0
    funding_model: Literal["accrue", "ignore_if_short"] = "accrue"

    @property
    def round_trip_bps(self) -> float:
        funding = self.funding_bps if self.funding_model == "accrue" else 0.0
        return self.fee_entry_bps + self.fee_exit_bps + self.slip_entry_bps + self.slip_exit_bps + funding


@dataclass(frozen=True)
class AISpec:
    model_id: str | None = None
    score: float = 0.0
    confidence: float = 0.0
    expected_net_bps: float = 0.0    # AFTER all costs — the hard gate reads this


@dataclass(frozen=True)
class TradePlan:
    side: Side
    decision_tf: str
    entry: EntrySpec
    risk: RiskSpec
    profit: ProfitSpec
    costs: CostSpec
    ai: AISpec
    source: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if self.side not in ("long", "short"):
            raise ValueError(f"side must be long/short, got {self.side!r}")
        if self.risk.stop_bps <= 0:
            raise ValueError("stop_bps must be positive — stop-less plans are forbidden")
        if not self.profit.targets:
            raise ValueError("at least one profit target is required")
        if sum(t.size_pct for t in self.profit.targets) > 100.0 + 1e-9:
            raise ValueError("target size_pct sums to > 100%")

    @property
    def tp1_bps(self) -> float:
        return min(t.bps for t in self.profit.targets)


def plan_gate(
    plan: TradePlan, cost_model: CostModel, *, safety_mult: float | None = None
) -> tuple[bool, list[str]]:
    """The hard cost gate. Returns (ok, reasons); ok=False ⇒ never trade.

    A plan is tradable only if its expected NET bps (after all costs) is positive
    AND its nearest target clears `safety_mult` × the full round-trip cost. When
    `safety_mult` is None it comes from the cost model's PROFILE (swing 2×, scalp
    3×, delta_scalp 3.5×) so a scalp lane must clear a bigger edge than a swing.
    """
    if safety_mult is None:
        safety_mult = cost_model.config.gate_safety_mult
    reasons: list[str] = []
    full_rt = plan.costs.round_trip_bps + cost_model.config.safety_buffer_bps
    if plan.ai.expected_net_bps <= 0:
        reasons.append(f"expected_net_bps <= 0 ({plan.ai.expected_net_bps:.1f})")
    if plan.tp1_bps < safety_mult * full_rt:
        reasons.append(
            f"TP1 {plan.tp1_bps:.1f}bps < {safety_mult}x round-trip cost {full_rt:.1f}bps"
        )
    return (not reasons, reasons)
