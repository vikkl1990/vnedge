"""Cost model — the single source of truth for fees + slippage + funding.

Research, paper, and live all price a trade with the SAME model. No strategy is
allowed a private fee assumption; a plan's cost fields are filled from here.
"""
from __future__ import annotations

from dataclasses import dataclass

# Canonical default fee/slip constants — the ONE source. The backtest FeeModel /
# SlippageModel and the paper FillModel default from these (see their modules),
# so research, paper, and the plan gate can't silently drift apart on costs.
DEFAULT_TAKER_FEE_BPS = 5.0     # per side, Binance USDT-M standard tier
DEFAULT_MAKER_FEE_BPS = 2.0
DEFAULT_SLIP_BPS = 2.0


@dataclass(frozen=True)
class CostModelConfig:
    taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS     # per side
    maker_fee_bps: float = DEFAULT_MAKER_FEE_BPS
    default_slip_entry_bps: float = DEFAULT_SLIP_BPS
    default_slip_exit_bps: float = DEFAULT_SLIP_BPS
    safety_buffer_bps: float = 3.0
    maker_adverse_bps: float = 1.0
    fee_gst_mult: float = 1.0
    funding_accrual: bool = True
    # lane profile: the platform runs swing + scalp families on the SAME contract
    # but with venue/hold-window-true costs. profile names the world; the fields
    # below make it real. gate_safety_mult is the plan_gate TP1 multiple (scalps
    # need a bigger edge to clear cost); free_exit_within_minutes models a venue
    # close discount (e.g. Delta India waives the exit fee inside 30 minutes).
    profile: str = "swing"
    gate_safety_mult: float = 2.0
    free_exit_within_minutes: float | None = None
    free_exit_fee_bps: float = 0.0


# Named cost worlds. One CostModel per lane profile — no private fee assumptions.
_SWING = CostModelConfig(profile="swing", gate_safety_mult=2.0)
_SCALP = CostModelConfig(
    profile="scalp", default_slip_entry_bps=2.0, default_slip_exit_bps=2.0,
    safety_buffer_bps=2.0, gate_safety_mult=3.0,
)
# delta_scalp defaults to full tariff + India GST + thinner-book slippage. The
# Scalper Offer is account/symbol/consent/hold dependent and is therefore not
# assumed by this generic profile; only an account-verified schedule may apply
# the close-leg waiver through the hybrid fee model.
_DELTA_SCALP = CostModelConfig(
    profile="delta_scalp", default_slip_entry_bps=3.0, default_slip_exit_bps=3.0,
    safety_buffer_bps=2.0, gate_safety_mult=3.5,
    maker_adverse_bps=1.5, fee_gst_mult=1.18,
)
COST_PROFILES: dict[str, CostModelConfig] = {
    "swing": _SWING, "scalp": _SCALP, "delta_scalp": _DELTA_SCALP,
}


class CostModel:
    def __init__(self, config: CostModelConfig | None = None) -> None:
        self.config = config or CostModelConfig()

    @classmethod
    def for_profile(cls, profile: str) -> CostModel:
        """CostModel for a named lane profile (swing / scalp / delta_scalp)."""
        try:
            return cls(COST_PROFILES[profile])
        except KeyError as exc:
            raise ValueError(
                f"unknown cost profile {profile!r}; known: {sorted(COST_PROFILES)}"
            ) from exc

    @property
    def profile(self) -> str:
        return self.config.profile

    def fee_bps(self, *, maker: bool = False) -> float:
        return self.config.maker_fee_bps if maker else self.config.taker_fee_bps

    def round_trip_bps(
        self, *, maker_entry: bool = False, maker_exit: bool = False,
        funding_bps: float = 0.0, include_safety: bool = True,
        hold_minutes: float | None = None,
    ) -> float:
        """fee_in + fee_out + slip_in + slip_out + funding + safety_buffer.

        ``hold_minutes`` applies a venue close discount: within
        ``free_exit_within_minutes`` the exit fee drops to ``free_exit_fee_bps``
        (Delta India's 30-minute rule). Omit it for a conservative full-fee
        estimate — the discount is only ever claimed when the hold is KNOWN
        short, never assumed.
        """
        c = self.config
        fee_out = self.fee_bps(maker=maker_exit)
        if (c.free_exit_within_minutes is not None and hold_minutes is not None
                and hold_minutes < c.free_exit_within_minutes):
            fee_out = c.free_exit_fee_bps
        funding = funding_bps if c.funding_accrual else 0.0
        fees = (self.fee_bps(maker=maker_entry) + fee_out) * c.fee_gst_mult
        rt = fees + c.default_slip_entry_bps + c.default_slip_exit_bps + funding
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
