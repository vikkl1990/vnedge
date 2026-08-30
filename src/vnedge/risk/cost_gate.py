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

from pydantic import BaseModel

from vnedge.plan.cost_model import (
    COST_PROFILES,
)
from vnedge.risk.fee_model import FeeModelPrediction

_MAKER = {"maker"}


def _dec(x: object) -> Decimal:
    """Coerce int/float/str/Decimal to Decimal without float-repr noise."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


class CostProfile(str, Enum):
    SCALP = "scalp"  # aggressive HF (Binance USDT-M)
    SWING = "swing"  # fallback / slower holds
    DELTA_SWING = "delta_swing"  # Delta India swing (18% GST, swing slippage)
    DELTA_SCALP = "delta_scalp"  # Delta India HF (adds 18% GST on fees)


class CostEstimate(BaseModel):
    model_config = {"frozen": True}
    fee_bps: Decimal  # round-trip exchange fee (GST-adjusted)
    slippage_bps: Decimal  # round-trip spread/impact + maker adverse selection
    funding_bps: Decimal  # signed: +cost for the paying side, -rebate for the other
    total_cost_bps: Decimal
    execution_model_id: str = "rules_only"
    execution_model_fallback: bool = True
    execution_model_reason: str | None = None
    execution_p90_bps: Decimal | None = None
    fee_schedule_id: str = "cost_gate_profile"
    fee_schedule_account_verified: bool = False
    fee_modifiers_applied: tuple[str, ...] = ()
    fee_schedule_reason: str | None = None


class CostGateResult(BaseModel):
    model_config = {"frozen": True}
    approved: bool
    expected_net_bps: Decimal
    min_required_bps: Decimal
    cost: CostEstimate
    available_room_bps: Decimal | None = None
    min_room_bps: Decimal | None = None
    reason: str | None = None  # only when rejected


class CostGateConfig(BaseModel):
    """Frozen so neither the profile nor the min-net threshold can be mutated at
    runtime — limit changes require reconstruction (a restart), like every risk config."""

    model_config = {"frozen": True}
    profile: CostProfile
    min_net_edge_bps: Decimal = Decimal("4.0")
    min_room_cost_multiple: Decimal = Decimal(0)


class CostGate:
    """Hard gate. Runs BEFORE sizing and the risk gateway."""

    def __init__(
        self,
        profile: CostProfile,
        min_net_edge_bps: Decimal = Decimal("4.0"),
        *,
        min_room_cost_multiple: Decimal = Decimal(0),
    ):
        room_multiple = _dec(min_room_cost_multiple)
        if room_multiple < 0:
            raise ValueError("min_room_cost_multiple cannot be negative")
        self.config = CostGateConfig(
            profile=profile,
            min_net_edge_bps=_dec(min_net_edge_bps),
            min_room_cost_multiple=room_multiple,
        )

    @property
    def profile(self) -> CostProfile:
        return self.config.profile

    @property
    def min_net_edge_bps(self) -> Decimal:
        return self.config.min_net_edge_bps

    @property
    def min_room_cost_multiple(self) -> Decimal:
        return self.config.min_room_cost_multiple

    def evaluate(
        self,
        signal_edge_bps: object,
        side: str,
        urgency: str,  # "maker" | "taker" | "aggressive"
        expected_holding_seconds: int,
        symbol: str,
        current_funding_rate: object = 0,
        available_room_bps: object | None = None,
        fee_model_prediction: FeeModelPrediction | None = None,
        predicted_funding_bps: object | None = None,
    ) -> CostGateResult:
        p = COST_PROFILES[self.profile.value]
        edge = _dec(signal_edge_bps)
        is_maker = urgency in _MAKER

        # --- Fees: round trip. Entry per urgency; exit pessimistic (taker close). ---
        maker_fee = _dec(p.maker_fee_bps)
        taker_fee = _dec(p.taker_fee_bps)
        entry_fee = maker_fee if is_maker else taker_fee
        exit_fee = taker_fee
        fee_bps = (entry_fee + exit_fee) * _dec(p.fee_gst_mult)

        # --- Slippage / adverse selection, round trip. ---
        aggressive = urgency == "aggressive"
        taker_slip = _dec(p.default_slip_exit_bps)
        exit_slip = taker_slip * (Decimal("1.5") if aggressive else Decimal(1))
        if is_maker:
            slippage_bps = _dec(p.maker_adverse_bps) + exit_slip
        else:
            entry_slip = _dec(p.default_slip_entry_bps) * (
                Decimal("1.5") if aggressive else Decimal(1)
            )
            slippage_bps = entry_slip + exit_slip  # cross both legs

        # Only a capital-safe prediction can bind. An account-verified fee
        # schedule may replace the generic tariff profile; otherwise the card
        # can only raise it. Execution cost always keeps the stricter floor.
        execution_model_id = "rules_only"
        execution_model_fallback = True
        execution_model_reason: str | None = None
        execution_p90_bps: Decimal | None = None
        fee_schedule_id = "cost_gate_profile"
        fee_schedule_account_verified = False
        fee_modifiers_applied: tuple[str, ...] = ()
        fee_schedule_reason: str | None = None
        prediction_context_matches = bool(
            fee_model_prediction is not None
            and fee_model_prediction.predicted_symbol.upper() == symbol.upper()
            and (
                "scalper_close_waiver"
                not in fee_model_prediction.schedule_modifiers_applied
                or fee_model_prediction.expected_holding_seconds
                == max(0, int(expected_holding_seconds))
            )
        )
        if (
            fee_model_prediction is not None
            and fee_model_prediction.capital_safe
            and prediction_context_matches
        ):
            if fee_model_prediction.schedule_account_verified:
                # Account statement/API truth is authoritative for tariff,
                # including verified temporary modifiers.  An unverified card
                # can only raise the generic profile, never lower it.
                fee_bps = fee_model_prediction.schedule_rt_bps
            else:
                fee_bps = max(fee_bps, fee_model_prediction.schedule_rt_bps)
            slippage_bps = max(
                slippage_bps,
                fee_model_prediction.exec_cost_for_gate_bps,
            )
            execution_model_id = fee_model_prediction.model_id
            execution_model_fallback = fee_model_prediction.fallback
            execution_model_reason = fee_model_prediction.fallback_reason
            execution_p90_bps = fee_model_prediction.ml_exec_p90_bps
            fee_schedule_id = fee_model_prediction.schedule_id
            fee_schedule_account_verified = fee_model_prediction.schedule_account_verified
            fee_modifiers_applied = fee_model_prediction.schedule_modifiers_applied
            fee_schedule_reason = fee_model_prediction.schedule_fallback_reason
        elif fee_model_prediction is not None:
            execution_model_id = "rules_only"
            execution_model_fallback = True
            execution_model_reason = (
                "fee_prediction_context_mismatch"
                if not prediction_context_matches
                else "unapproved_execution_model_ignored"
            )

        # Funding is a discrete inventory cash flow at a SETTLED venue print.
        # Never pro-rate the current/predicted ticker rate over elapsed time and
        # never infer an 8h clock here.  A caller may supply a separately
        # computed, side-aware haircut only when a known settlement timestamp
        # lies inside the frozen expected hold.  Unknown funding is zero, not
        # an entry blocker.  ``current_funding_rate`` remains as a compatibility
        # input for older callers but is intentionally telemetry-only.
        del current_funding_rate
        funding_bps = (
            Decimal(0)
            if predicted_funding_bps is None
            else _dec(predicted_funding_bps)
        )

        total = fee_bps + slippage_bps + funding_bps
        net = edge - total
        edge_ok = net >= self.min_net_edge_bps
        room = None if available_room_bps is None else _dec(available_room_bps)
        # A favorable funding estimate may improve net EV, but it must never
        # shrink the physical fee/slippage wall that price still has to clear.
        room_cost_wall = fee_bps + slippage_bps + max(funding_bps, Decimal(0))
        min_room = (
            room_cost_wall * self.min_room_cost_multiple
            if self.min_room_cost_multiple > 0
            else None
        )
        room_ok = min_room is None or (room is not None and room >= min_room)
        approved = edge_ok and room_ok
        reasons: list[str] = []
        if not edge_ok:
            reasons.append(
                f"net {net:.2f}bps < min {self.min_net_edge_bps}bps "
                f"(edge {edge:.2f} − cost {total:.2f}: fee {fee_bps:.2f}, "
                f"slip {slippage_bps:.2f}, funding {funding_bps:.2f})"
            )
        if not room_ok:
            if room is None:
                reasons.append(
                    f"room missing; requires at least {min_room:.2f}bps "
                    f"({self.min_room_cost_multiple}× cost wall)"
                )
            else:
                reasons.append(
                    f"room {room:.2f}bps < min {min_room:.2f}bps "
                    f"({self.min_room_cost_multiple}× cost wall)"
                )
        return CostGateResult(
            approved=approved,
            expected_net_bps=net,
            min_required_bps=self.min_net_edge_bps,
            available_room_bps=room,
            min_room_bps=min_room,
            cost=CostEstimate(
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                funding_bps=funding_bps,
                total_cost_bps=total,
                execution_model_id=execution_model_id,
                execution_model_fallback=execution_model_fallback,
                execution_model_reason=execution_model_reason,
                execution_p90_bps=execution_p90_bps,
                fee_schedule_id=fee_schedule_id,
                fee_schedule_account_verified=fee_schedule_account_verified,
                fee_modifiers_applied=fee_modifiers_applied,
                fee_schedule_reason=fee_schedule_reason,
            ),
            reason="; ".join(reasons) if reasons else None,
        )
