"""Predictable entry engine — an explicit, measurable entry policy.

Replaces ad-hoc "next bar open always" with three modes; every attempt reports
its bps slippage vs the signal price, and adverse excursion beyond the plan's
max is a cancel, never a chase. Pure per-step evaluation — the caller drives the
loop and supplies current state; the engine holds no order-placement logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from vnedge.plan.cost_model import CostModel
from vnedge.plan.trade_plan import TradePlan, bps_frac


@dataclass(frozen=True)
class EntryResult:
    status: Literal["filled", "reject", "timeout", "waiting"]
    fill_price: float | None = None
    slippage_bps: float | None = None   # signed adverse slip vs signal (+ = worse)
    reason: str = ""


class EntryEngine:
    def __init__(self, cost_model: CostModel | None = None) -> None:
        self.cost_model = cost_model or CostModel()

    def evaluate(
        self,
        plan: TradePlan,
        signal_price: float,
        *,
        next_open: float | None = None,
        current_price: float | None = None,
        ltf_confirmed: bool = False,
        bars_elapsed: int = 0,
    ) -> EntryResult:
        e, side = plan.entry, plan.side

        def adverse_slip(fill: float) -> float:
            raw = (fill / signal_price - 1.0) * 1e4
            return raw if side == "long" else -raw   # positive == worse fill

        if e.type == "next_open":
            if next_open is None:
                return EntryResult("waiting", reason="awaiting next open")
            s = adverse_slip(next_open)
            if s > e.max_entry_slip_bps:
                return EntryResult("reject", slippage_bps=s, reason="entry slip exceeds max")
            return EntryResult("filled", fill_price=next_open, slippage_bps=s, reason="next_open")

        if e.type == "limit_bps":
            if e.limit_offset_bps is None:
                return EntryResult("reject", reason="limit_offset_bps missing")
            limit = (signal_price * (1 - bps_frac(e.limit_offset_bps)) if side == "long"
                     else signal_price * (1 + bps_frac(e.limit_offset_bps)))
            reached = current_price is not None and (
                (side == "long" and current_price <= limit)
                or (side == "short" and current_price >= limit)
            )
            if reached:
                return EntryResult("filled", fill_price=limit,
                                   slippage_bps=adverse_slip(limit), reason="limit filled")
            if bars_elapsed >= e.entry_timeout_bars:
                return EntryResult("timeout", reason="limit not filled in time")
            return EntryResult("waiting", reason="limit resting")

        if e.type == "ltf_confirm":
            if ltf_confirmed and current_price is not None:
                s = adverse_slip(current_price)
                if s > e.max_entry_slip_bps:
                    return EntryResult("reject", slippage_bps=s, reason="entry slip exceeds max")
                return EntryResult("filled", fill_price=current_price, slippage_bps=s,
                                   reason="ltf confirmed")
            if bars_elapsed >= e.entry_timeout_bars:
                return EntryResult("timeout", reason="no ltf confirmation in time")
            return EntryResult("waiting", reason="awaiting ltf confirmation")

        return EntryResult("reject", reason=f"unknown entry type {e.type!r}")
