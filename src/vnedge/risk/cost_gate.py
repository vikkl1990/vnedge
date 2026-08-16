"""CostGate — the HARD, non-bypassable cost filter for the HF path.

Runs BEFORE position sizing and the risk gateway. Rejects any signal whose
estimated edge cannot cover the round-trip cost plus a minimum net margin. This
is the structural answer to the cost wall the research found (raw flow edge
~0.3bps vs a ~8bps taker wall): nothing reaches sizing unless it clears cost.

Hybrid execution (the bot decides maker vs taker per trade): ``urgency`` selects
the ENTRY fee (maker vs taker), while the EXIT is costed pessimistically at the
taker fee — a stop / emergency close crosses the book. A pure maker-exit trade is
therefore slightly UNDER-approved, which is the capital-protective direction.

Pure evaluator: it never journals or mutates. The caller drops a rejected intent
and journals ``cost_rejected`` (OrderManager.submit invariant).
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from vnedge.plan.cost_model import (
    DEFAULT_MAKER_FEE_BPS,
    DEFAULT_SLIP_BPS,
    DEFAULT_TAKER_FEE_BPS,
)

# Perp funding accrues per interval (Binance / Delta India = 8h).
_FUNDING_INTERVAL_SECONDS = Decimal(8 * 3600)
_MAKER = {"maker"}
_LONG = {"buy", "long"}


def _dec(x: object) -> Decimal:
    """Coerce int/float/str/Decimal to Decimal without float-repr noise."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


class CostProfile(str, Enum):
    SCALP = "scalp"                # aggressive HF (Binance USDT-M)
    SWING = "swing"               # fallback / slower holds
    DELTA_SCALP = "delta_scalp"   # Delta India HF (adds 18% GST on fees)


class _ProfileParams(BaseModel):
    model_config = {"frozen": True}
    maker_fee_bps: Decimal        # per side
    taker_fee_bps: Decimal        # per side
    taker_slip_bps: Decimal       # spread cross + impact per taker leg
    maker_adverse_bps: Decimal    # adverse selection suffered on a posted (maker) leg
    fee_gst_mult: Decimal         # India GST on the exchange fee (1.0 = none)


_M = _dec(DEFAULT_MAKER_FEE_BPS)
_T = _dec(DEFAULT_TAKER_FEE_BPS)
_S = _dec(DEFAULT_SLIP_BPS)

_PROFILES: dict[CostProfile, _ProfileParams] = {
    CostProfile.SCALP: _ProfileParams(
        maker_fee_bps=_M, taker_fee_bps=_T, taker_slip_bps=_S,
        maker_adverse_bps=Decimal("1.0"), fee_gst_mult=Decimal("1.0"),
    ),
    CostProfile.SWING: _ProfileParams(
        maker_fee_bps=_M, taker_fee_bps=_T, taker_slip_bps=_S,
        maker_adverse_bps=Decimal("1.0"), fee_gst_mult=Decimal("1.0"),
    ),
    # Delta India: thinner books (higher slip) + 18% GST on the fee itself.
    CostProfile.DELTA_SCALP: _ProfileParams(
        maker_fee_bps=_M, taker_fee_bps=_T, taker_slip_bps=Decimal("3.0"),
        maker_adverse_bps=Decimal("1.5"), fee_gst_mult=Decimal("1.18"),
    ),
}


class CostEstimate(BaseModel):
    model_config = {"frozen": True}
    fee_bps: Decimal            # round-trip exchange fee (GST-adjusted)
    slippage_bps: Decimal       # round-trip spread/impact + maker adverse selection
    funding_bps: Decimal        # signed: +cost for the paying side, -rebate for the other
    total_cost_bps: Decimal


class CostGateResult(BaseModel):
    model_config = {"frozen": True}
    approved: bool
    expected_net_bps: Decimal
    min_required_bps: Decimal
    cost: CostEstimate
    reason: Optional[str] = None   # only when rejected


class CostGate:
    """Hard gate. Runs BEFORE sizing and the risk gateway."""

    def __init__(self, profile: CostProfile, min_net_edge_bps: Decimal = Decimal("4.0")):
        self.profile = profile
        self.min_net_edge_bps = _dec(min_net_edge_bps)

    def evaluate(
        self,
        signal_edge_bps: object,
        side: str,
        urgency: str,                     # "maker" | "taker" | "aggressive"
        expected_holding_seconds: int,
        current_funding_rate: object,     # per-interval fraction, e.g. 0.0001 = 1bp/8h
        symbol: str,
    ) -> CostGateResult:
        p = _PROFILES[self.profile]
        edge = _dec(signal_edge_bps)
        is_maker = urgency in _MAKER

        # --- Fees: round trip. Entry per urgency; exit pessimistic (taker close). ---
        entry_fee = p.maker_fee_bps if is_maker else p.taker_fee_bps
        exit_fee = p.taker_fee_bps
        fee_bps = (entry_fee + exit_fee) * p.fee_gst_mult

        # --- Slippage / adverse selection, round trip. ---
        aggressive = urgency == "aggressive"
        exit_slip = p.taker_slip_bps * (Decimal("1.5") if aggressive else Decimal("1"))
        if is_maker:
            slippage_bps = p.maker_adverse_bps + exit_slip     # post entry, cross exit
        else:
            entry_slip = p.taker_slip_bps * (Decimal("1.5") if aggressive else Decimal("1"))
            slippage_bps = entry_slip + exit_slip              # cross both legs

        # --- Funding over the expected hold (signed). Long pays +funding; short earns it. ---
        hold = Decimal(max(0, int(expected_holding_seconds)))
        funding_frac = _dec(current_funding_rate) * (hold / _FUNDING_INTERVAL_SECONDS)
        funding_signed = funding_frac * Decimal(10000)         # -> bps
        funding_bps = funding_signed if side in _LONG else -funding_signed

        total = fee_bps + slippage_bps + funding_bps
        net = edge - total
        approved = net >= self.min_net_edge_bps
        reason = None
        if not approved:
            reason = (
                f"net {net:.2f}bps < min {self.min_net_edge_bps}bps "
                f"(edge {edge:.2f} − cost {total:.2f}: fee {fee_bps:.2f}, "
                f"slip {slippage_bps:.2f}, funding {funding_bps:.2f})"
            )
        return CostGateResult(
            approved=approved,
            expected_net_bps=net,
            min_required_bps=self.min_net_edge_bps,
            cost=CostEstimate(
                fee_bps=fee_bps, slippage_bps=slippage_bps,
                funding_bps=funding_bps, total_cost_bps=total,
            ),
            reason=reason,
        )
