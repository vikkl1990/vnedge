"""Cost model — the single source of truth for fees + slippage + funding.

Research, paper, and live all price a trade with the SAME model. No strategy is
allowed a private fee assumption; a plan's cost fields are filled from here.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModelConfig:
    taker_fee_bps: float = 5.0          # per side
    maker_fee_bps: float = 2.0
    default_slip_entry_bps: float = 2.0
    default_slip_exit_bps: float = 2.0
    safety_buffer_bps: float = 3.0
    funding_accrual: bool = True


class CostModel:
    def __init__(self, config: CostModelConfig | None = None) -> None:
        self.config = config or CostModelConfig()

    def fee_bps(self, *, maker: bool = False) -> float:
        return self.config.maker_fee_bps if maker else self.config.taker_fee_bps

    def round_trip_bps(
        self, *, maker_entry: bool = False, maker_exit: bool = False,
        funding_bps: float = 0.0, include_safety: bool = True,
    ) -> float:
        """fee_in + fee_out + slip_in + slip_out + funding + safety_buffer."""
        c = self.config
        funding = funding_bps if c.funding_accrual else 0.0
        rt = (self.fee_bps(maker=maker_entry) + self.fee_bps(maker=maker_exit)
              + c.default_slip_entry_bps + c.default_slip_exit_bps + funding)
        if include_safety:
            rt += c.safety_buffer_bps
        return rt

    def net_bps(
        self, gross_bps: float, *, funding_bps: float = 0.0,
        maker_entry: bool = False, maker_exit: bool = False,
    ) -> float:
        """gross_bps (signed realized move) minus full round-trip cost."""
        return gross_bps - self.round_trip_bps(
            maker_entry=maker_entry, maker_exit=maker_exit, funding_bps=funding_bps
        )
