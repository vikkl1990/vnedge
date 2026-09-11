"""Cost model — the single source of truth for fees + slippage + funding.

Research, paper, and live all price a trade with the SAME model. No strategy is
allowed a private fee assumption; a plan's cost fields are filled from here.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from decimal import Decimal

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
    execution_policy: str = "legacy_v1"


# Named cost worlds. One CostModel per lane profile — no private fee assumptions.
_SWING = CostModelConfig(profile="swing", gate_safety_mult=2.0)
# Delta swing keeps the slower-lane execution assumptions (2 bps slippage per
# leg and a 3 bps gate reserve) while applying the venue's real 18% GST to its
# 5 bps taker / 2 bps maker tariff.  It is intentionally separate from
# ``delta_scalp``: a swing lane must not silently inherit a thinner-book scalp
# slippage model merely to get the tax right.
_DELTA_SWING = CostModelConfig(
    profile="delta_swing",
    gate_safety_mult=2.0,
    fee_gst_mult=1.18,
)
# Pair-scoped cost identities intentionally begin with the same conservative
# Delta tariff/slippage vector.  The distinct IDs prevent later measured BTC
# or ETH execution changes from rewriting the other pair's evidence stream.
# Changing either vector requires a new profile version.
_DELTA_SWING_BTC_V1 = replace(_DELTA_SWING, profile="delta_swing_btc_v1")
_DELTA_SWING_ETH_V1 = replace(_DELTA_SWING, profile="delta_swing_eth_v1")
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
# Opt-in correction, not an alias: existing experiments retain their old bills.
# Maker adverse selection is a floor, not a discount against entry impact.
_DELTA_SCALP_V2 = replace(
    _DELTA_SCALP, profile="delta_scalp_v2", execution_policy="conservative_shared_v2",
)
COST_PROFILES: dict[str, CostModelConfig] = {
    "swing": _SWING,
    "delta_swing": _DELTA_SWING,
    "delta_swing_btc_v1": _DELTA_SWING_BTC_V1,
    "delta_swing_eth_v1": _DELTA_SWING_ETH_V1,
    "scalp": _SCALP,
    "delta_scalp": _DELTA_SCALP,
    "delta_scalp_v2": _DELTA_SCALP_V2,
}


class CostModel:
    def __init__(self, config: CostModelConfig | None = None) -> None:
        self.config = config or CostModelConfig()

    @classmethod
    def for_profile(cls, profile: str) -> CostModel:
        """CostModel for a named lane/venue cost profile."""
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

    @property
    def config_sha256(self) -> str:
        payload = json.dumps(asdict(self.config), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def execution_bps(
        self, *, maker_entry: bool = False, aggressive: bool = False,
        legacy_gate: bool = False,
    ) -> Decimal:
        """Estimated round-trip impact; NOT a charge to subtract from fills.

        Legacy gates used a cheaper maker floor than legacy research. Preserve
        those frozen paths explicitly. New v2 profiles share the stricter floor.
        """
        c = self.config
        entry = Decimal(str(c.default_slip_entry_bps))
        exit_ = Decimal(str(c.default_slip_exit_bps))
        adverse = Decimal(str(c.maker_adverse_bps))
        if c.execution_policy == "conservative_shared_v2":
            if maker_entry:
                entry = max(entry, adverse)
        elif c.execution_policy == "legacy_v1":
            if legacy_gate and maker_entry:
                entry = adverse
        else:
            raise ValueError(f"unknown execution policy: {c.execution_policy}")
        if aggressive:
            entry *= Decimal("1.5")
            exit_ *= Decimal("1.5")
        return entry + exit_

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
        if c.execution_policy == "legacy_v1":
            # Preserve historical float operation order as well as assumptions.
            rt = fees + c.default_slip_entry_bps + c.default_slip_exit_bps + funding
        else:
            rt = fees + float(self.execution_bps(maker_entry=maker_entry)) + funding
        if include_safety:
            rt += c.safety_buffer_bps
        return rt

    def net_bps(
        self, gross_bps: float, *, funding_bps: float = 0.0,
        maker_entry: bool = False, maker_exit: bool = False,
    ) -> float:
        """Estimated research net: gross move minus modeled costs only.

        Do not apply this to actual fill-to-fill PnL: execution prices already
        contain spread/impact. Actual charges require cash reconciliation.

        The safety reserve belongs to pre-trade gating and is never charged to
        the account. Call ``round_trip_bps(include_safety=True)`` explicitly
        when displaying the research wall.
        """
        return gross_bps - self.round_trip_bps(
            maker_entry=maker_entry,
            maker_exit=maker_exit,
            funding_bps=funding_bps,
            include_safety=False,
        )
