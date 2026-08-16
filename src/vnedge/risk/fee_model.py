"""Deterministic fee schedule plus conservative execution-cost estimates.

Exchange tariffs, tax and account offers are rules.  A learned model is only
allowed to estimate the uncertain residual (spread, slippage, impact and maker
adverse selection).  The capital-facing value is always the greater of the
model's P90 estimate and the configured rules floor.

This module does not place orders or grant strategy eligibility.  ``CostGate``
remains the hard pre-sizing authority and also retains its own rules-only floor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol

Venue = Literal["delta_india", "binanceusdm", "bybit"]
Liquidity = Literal["taker", "maker"]
FeeLeg = Literal["open", "close"]
OrderSide = Literal["buy", "sell"]
DataQuality = Literal["ok", "degraded", "gap"]

FEATURE_SCHEMA_VERSION = "execution_cost_v1"


def _decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class FeeCalculation:
    liquidity: Liquidity
    leg: FeeLeg
    base_fee_bps: Decimal
    discounted_fee_bps: Decimal
    all_in_fee_bps: Decimal
    applied_modifiers: tuple[str, ...]
    fallback_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScalperOfferRule:
    """Account-confirmed close-waiver window for one normalized symbol."""

    symbol: str
    max_hold_seconds: int

    def __post_init__(self) -> None:
        normalized = self.symbol.strip().upper()
        if not normalized:
            raise ValueError("scalper offer symbol is required")
        if isinstance(self.max_hold_seconds, bool) or self.max_hold_seconds <= 0:
            raise ValueError("scalper offer window must be positive seconds")
        object.__setattr__(self, "symbol", normalized)


@dataclass(frozen=True, slots=True)
class RoundTripFeeCalculation:
    entry: FeeCalculation
    exit: FeeCalculation

    @property
    def total_bps(self) -> Decimal:
        return self.entry.all_in_fee_bps + self.exit.all_in_fee_bps

    @property
    def applied_modifiers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.entry.applied_modifiers, *self.exit.applied_modifiers)))

    @property
    def fallback_reason(self) -> str | None:
        reasons = tuple(
            dict.fromkeys(
                reason
                for reason in (self.entry.fallback_reason, self.exit.fallback_reason)
                if reason is not None
            )
        )
        return ";".join(reasons) if reasons else None


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """An account-verified fee card; rates are per leg in basis points.

    Scalper Offer is modeled as a qualifying *close-leg waiver*, never a global
    percentage discount. DETO is a multiplier on a nonzero charged leg. Active
    but unverified modifiers, stale state, or unverified stacking are ignored.
    """

    taker_bps: Decimal
    maker_bps: Decimal
    gst: Decimal = Decimal(0)
    scalper_offer_active: bool = False
    scalper_consent: bool = False
    scalper_rules: tuple[ScalperOfferRule, ...] = ()
    deto_discount_active: bool = False
    deto_mult: Decimal = Decimal("0.75")
    account_verified: bool = False
    discounts_verified: bool = False
    discounts_stack_verified: bool = False
    verification_id: str | None = None
    discounts_verified_until: datetime | None = None
    schedule_id: str = "account_schedule_unversioned"

    def __post_init__(self) -> None:
        for name in ("taker_bps", "maker_bps", "gst", "deto_mult"):
            value = _decimal(getattr(self, name), name=name)
            object.__setattr__(self, name, value)
        if self.taker_bps < 0 or self.maker_bps < 0 or self.gst < 0:
            raise ValueError("fee rates and GST cannot be negative")
        if not Decimal(0) <= self.deto_mult <= Decimal(1):
            raise ValueError("deto_mult must be within [0, 1]")
        if not self.schedule_id.strip():
            raise ValueError("schedule_id is required for auditability")
        boolean_fields = (
            "scalper_offer_active",
            "scalper_consent",
            "deto_discount_active",
            "account_verified",
            "discounts_verified",
            "discounts_stack_verified",
        )
        if any(type(getattr(self, name)) is not bool for name in boolean_fields):
            raise TypeError("fee schedule state flags must be bool")
        if any(not isinstance(rule, ScalperOfferRule) for rule in self.scalper_rules):
            raise TypeError("scalper_rules must contain ScalperOfferRule values")
        rule_symbols = [rule.symbol for rule in self.scalper_rules]
        if len(rule_symbols) != len(set(rule_symbols)):
            raise ValueError("scalper offer rules must have unique symbols")
        if self.account_verified and not (self.verification_id or "").strip():
            raise ValueError("account_verified schedules require verification_id")
        if self.discounts_verified and not self.account_verified:
            raise ValueError("discount verification requires an account-verified schedule")
        if self.discounts_verified:
            expires = self.discounts_verified_until
            if expires is None or expires.tzinfo is None or expires.utcoffset() is None:
                raise ValueError("verified discounts require a timezone-aware expiry")
            object.__setattr__(self, "discounts_verified_until", expires.astimezone(UTC))
        elif self.discounts_verified_until is not None:
            raise ValueError("discount expiry requires discounts_verified=True")
        if self.discounts_stack_verified and not (
            self.discounts_verified
            and self.scalper_offer_active
            and self.deto_discount_active
        ):
            raise ValueError(
                "stack verification requires both active discounts and verified account state"
            )

    def fee_bps(
        self,
        liquidity: Liquidity,
        *,
        leg: FeeLeg = "open",
        symbol: str | None = None,
        hold_seconds: int | None = None,
        as_of: datetime | None = None,
    ) -> Decimal:
        return self.calculation(
            liquidity,
            leg=leg,
            symbol=symbol,
            hold_seconds=hold_seconds,
            as_of=as_of,
        ).all_in_fee_bps

    def calculation(
        self,
        liquidity: Liquidity,
        *,
        leg: FeeLeg = "open",
        symbol: str | None = None,
        hold_seconds: int | None = None,
        as_of: datetime | None = None,
    ) -> FeeCalculation:
        if liquidity not in {"maker", "taker"}:
            raise ValueError("liquidity must be 'maker' or 'taker'")
        if leg not in {"open", "close"}:
            raise ValueError("fee leg must be 'open' or 'close'")
        if hold_seconds is not None and (
            isinstance(hold_seconds, bool) or hold_seconds < 0
        ):
            raise ValueError("hold_seconds must be non-negative")
        evaluation_time: datetime | None = None
        if as_of is not None:
            if as_of.tzinfo is None or as_of.utcoffset() is None:
                raise ValueError("fee calculation as_of must be timezone-aware")
            evaluation_time = as_of.astimezone(UTC)
        base = self.taker_bps if liquidity == "taker" else self.maker_bps
        discounted = base
        applied_list: list[str] = []
        fallback_reason: str | None = None
        any_discount = self.scalper_offer_active or self.deto_discount_active
        discounts_expired = bool(
            self.discounts_verified
            and evaluation_time is not None
            and self.discounts_verified_until is not None
            and evaluation_time > self.discounts_verified_until
        )
        if any_discount and not self.discounts_verified:
            fallback_reason = "discount_state_unverified"
        elif any_discount and discounts_expired:
            fallback_reason = "discount_verification_expired"
        elif (
            self.scalper_offer_active
            and self.deto_discount_active
            and not self.discounts_stack_verified
        ):
            fallback_reason = "discount_stacking_unverified"
        else:
            close_waived = False
            if self.scalper_offer_active and leg == "close":
                normalized_symbol = (symbol or "").strip().upper()
                rule = next(
                    (item for item in self.scalper_rules if item.symbol == normalized_symbol),
                    None,
                )
                if not self.scalper_consent:
                    fallback_reason = "scalper_consent_missing"
                elif not normalized_symbol or hold_seconds is None:
                    fallback_reason = "scalper_context_missing"
                elif rule is None:
                    fallback_reason = "scalper_symbol_ineligible"
                elif hold_seconds <= rule.max_hold_seconds:
                    discounted = Decimal(0)
                    close_waived = True
                    applied_list.append("scalper_close_waiver")
                else:
                    fallback_reason = "scalper_window_exceeded"
            if self.deto_discount_active and not close_waived:
                discounted *= self.deto_mult
                applied_list.append("deto")
        return FeeCalculation(
            liquidity=liquidity,
            leg=leg,
            base_fee_bps=base,
            discounted_fee_bps=discounted,
            all_in_fee_bps=discounted * (Decimal(1) + self.gst),
            applied_modifiers=tuple(applied_list),
            fallback_reason=fallback_reason,
        )

    def round_trip_calculation(
        self,
        entry: Liquidity,
        exit: Liquidity,
        *,
        symbol: str | None = None,
        hold_seconds: int | None = None,
        as_of: datetime | None = None,
    ) -> RoundTripFeeCalculation:
        return RoundTripFeeCalculation(
            entry=self.calculation(entry, leg="open", as_of=as_of),
            exit=self.calculation(
                exit,
                leg="close",
                symbol=symbol,
                hold_seconds=hold_seconds,
                as_of=as_of,
            ),
        )

    def round_trip_bps(
        self,
        entry: Liquidity,
        exit: Liquidity,
        *,
        symbol: str | None = None,
        hold_seconds: int | None = None,
        as_of: datetime | None = None,
    ) -> Decimal:
        return self.round_trip_calculation(
            entry,
            exit,
            symbol=symbol,
            hold_seconds=hold_seconds,
            as_of=as_of,
        ).total_bps


# Reference cards preserve the repo's current configured assumptions.  They are
# deliberately named REFERENCE: deployment must select/version the card backed
# by the operator's live account statement.
BINANCEUSDM_REFERENCE = FeeSchedule(
    taker_bps=Decimal(5),
    maker_bps=Decimal(2),
    schedule_id="binanceusdm_repo_reference_v1",
)
BYBIT_REFERENCE = FeeSchedule(
    taker_bps=Decimal("5.5"),
    maker_bps=Decimal(2),
    schedule_id="bybit_repo_reference_v1",
)
DELTA_INDIA_REFERENCE = FeeSchedule(
    taker_bps=Decimal(5),
    maker_bps=Decimal(2),
    gst=Decimal("0.18"),
    schedule_id="delta_india_repo_reference_v1",
)


@dataclass(frozen=True, slots=True)
class ExecutionCostFeatures:
    """Frozen, at-order-time feature vector with no future information."""

    observed_at: datetime
    venue: Venue
    symbol: str
    urgency: Liquidity
    side: OrderSide
    spread_bps: Decimal
    atr_1h_bps: Decimal
    volume_rank_24h: Decimal
    size_notional_usd: Decimal
    data_quality: DataQuality
    hour_utc: int
    session: str
    book_imbalance: Decimal | None = None
    schema_version: str = FEATURE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))
        if self.venue not in {"delta_india", "binanceusdm", "bybit"}:
            raise ValueError("unsupported execution-cost venue")
        if self.urgency not in {"maker", "taker"} or self.side not in {"buy", "sell"}:
            raise ValueError("invalid urgency or side")
        if self.data_quality not in {"ok", "degraded", "gap"}:
            raise ValueError("invalid data_quality")
        if not self.symbol.strip() or not self.session.strip():
            raise ValueError("symbol and session are required")
        if self.schema_version != FEATURE_SCHEMA_VERSION:
            raise ValueError(f"unsupported feature schema: {self.schema_version}")
        if not 0 <= self.hour_utc <= 23:
            raise ValueError("hour_utc must be within [0, 23]")
        if self.hour_utc != self.observed_at.hour:
            raise ValueError("hour_utc must match observed_at in UTC")
        for name in ("spread_bps", "atr_1h_bps", "volume_rank_24h", "size_notional_usd"):
            value = _decimal(getattr(self, name), name=name)
            object.__setattr__(self, name, value)
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if not Decimal(0) <= self.volume_rank_24h <= Decimal(1):
            raise ValueError("volume_rank_24h must be within [0, 1]")
        if self.book_imbalance is not None:
            imbalance = _decimal(self.book_imbalance, name="book_imbalance")
            if not Decimal(-1) <= imbalance <= Decimal(1):
                raise ValueError("book_imbalance must be within [-1, 1]")
            object.__setattr__(self, "book_imbalance", imbalance)

    def model_row(self) -> dict[str, object]:
        """JSON/sklearn-friendly row; timestamp is identity, not a predictor."""
        return {
            "venue": self.venue,
            "symbol": self.symbol.upper(),
            "urgency": self.urgency,
            "side": self.side,
            "spread_bps": float(self.spread_bps),
            "book_imbalance": (
                float(self.book_imbalance) if self.book_imbalance is not None else 0.0
            ),
            "book_imbalance_available": int(self.book_imbalance is not None),
            "atr_1h_bps": float(self.atr_1h_bps),
            "volume_rank_24h": float(self.volume_rank_24h),
            "hour_utc": self.hour_utc,
            "session": self.session,
            "size_notional_usd": float(self.size_notional_usd),
            "data_quality": self.data_quality,
        }

    def journal_fields(self) -> dict[str, object]:
        row = self.model_row()
        row.update(
            observed_at=self.observed_at.isoformat(),
            feature_schema_version=self.schema_version,
        )
        return row


class ExecutionCostQuantileModel(Protocol):
    model_id: str
    trained_at: datetime
    feature_schema_version: str
    runtime_approved: bool

    def predict_quantiles(self, features: ExecutionCostFeatures) -> tuple[object, object]:
        """Return round-trip execution residual P50 and P90 in basis points."""


@dataclass(frozen=True, slots=True)
class FeeModelPrediction:
    schedule_rt_bps: Decimal
    ml_exec_rt_bps: Decimal
    ml_exec_p90_bps: Decimal
    exec_floor_rt_bps: Decimal
    exec_cost_for_gate_bps: Decimal
    total_rt_bps: Decimal
    model_id: str
    schedule_id: str
    schedule_account_verified: bool
    schedule_modifiers_applied: tuple[str, ...]
    schedule_fallback_reason: str | None
    predicted_symbol: str
    expected_holding_seconds: int | None
    fallback: bool
    capital_safe: bool
    fallback_reason: str | None = None
    feature_schema_version: str = FEATURE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        numeric = (
            "schedule_rt_bps",
            "ml_exec_rt_bps",
            "ml_exec_p90_bps",
            "exec_floor_rt_bps",
            "exec_cost_for_gate_bps",
            "total_rt_bps",
        )
        for name in numeric:
            value = _decimal(getattr(self, name), name=name)
            object.__setattr__(self, name, value)
        if any(getattr(self, name) < 0 for name in numeric if name != "ml_exec_rt_bps"):
            raise ValueError("capital-facing fee prediction values cannot be negative")
        if self.exec_cost_for_gate_bps < self.exec_floor_rt_bps:
            raise ValueError("execution cost cannot be below the rules floor")
        if self.total_rt_bps != self.schedule_rt_bps + self.exec_cost_for_gate_bps:
            raise ValueError("total_rt_bps must equal schedule plus gated execution cost")
        if not self.model_id.strip() or not self.schedule_id.strip():
            raise ValueError("model_id and schedule_id are required")
        if any(
            type(value) is not bool
            for value in (
                self.fallback,
                self.capital_safe,
                self.schedule_account_verified,
            )
        ):
            raise TypeError("prediction state flags must be bool")
        if any(
            name not in {"scalper_close_waiver", "deto"}
            for name in self.schedule_modifiers_applied
        ):
            raise ValueError("unsupported fee schedule modifier")
        if not self.predicted_symbol.strip():
            raise ValueError("predicted_symbol is required")
        if self.expected_holding_seconds is not None and (
            isinstance(self.expected_holding_seconds, bool)
            or self.expected_holding_seconds < 0
        ):
            raise ValueError("expected_holding_seconds must be non-negative")
        if self.feature_schema_version != FEATURE_SCHEMA_VERSION:
            raise ValueError("unsupported prediction feature schema")


class HybridFeeModel:
    """Rules schedule plus an optional, freshness-gated quantile model."""

    def __init__(
        self,
        schedule: FeeSchedule,
        ml: ExecutionCostQuantileModel | None = None,
        *,
        floor_slip_bps_per_leg: Decimal = Decimal(2),
        max_model_age: timedelta = timedelta(days=30),
        drift_floor_multiplier: Decimal = Decimal("1.5"),
    ) -> None:
        floor = _decimal(floor_slip_bps_per_leg, name="floor_slip_bps_per_leg")
        multiplier = _decimal(drift_floor_multiplier, name="drift_floor_multiplier")
        if floor < 0 or multiplier < 1:
            raise ValueError("execution floor must be non-negative and drift multiplier >= 1")
        if max_model_age <= timedelta(0):
            raise ValueError("max_model_age must be positive")
        self.schedule = schedule
        self.ml = ml
        self.floor_slip_bps_per_leg = floor
        self.max_model_age = max_model_age
        self.drift_floor_multiplier = multiplier

    def schedule_rt(
        self,
        entry: Liquidity,
        exit: Liquidity,
        *,
        symbol: str | None = None,
        expected_holding_seconds: int | None = None,
        as_of: datetime | None = None,
    ) -> Decimal:
        return self.schedule.round_trip_bps(
            entry,
            exit,
            symbol=symbol,
            hold_seconds=expected_holding_seconds,
            as_of=as_of,
        )

    def predict(
        self,
        features: ExecutionCostFeatures,
        entry: Liquidity,
        exit: Liquidity,
        *,
        expected_holding_seconds: int | None = None,
        as_of: datetime | None = None,
        drift_alarm: bool = False,
        shadow: bool = False,
    ) -> FeeModelPrediction:
        if features.urgency != entry:
            raise ValueError("feature urgency must match entry liquidity")
        evaluation_time = as_of or features.observed_at
        if evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        now = evaluation_time.astimezone(UTC)
        if now < features.observed_at:
            raise ValueError("as_of cannot precede the feature snapshot")
        if expected_holding_seconds is not None and (
            isinstance(expected_holding_seconds, bool) or expected_holding_seconds < 0
        ):
            raise ValueError("expected_holding_seconds must be non-negative")
        schedule_calculation = self.schedule.round_trip_calculation(
            entry,
            exit,
            symbol=features.symbol,
            hold_seconds=expected_holding_seconds,
            as_of=now,
        )
        schedule_rt = schedule_calculation.total_bps
        base_floor = self.floor_slip_bps_per_leg * Decimal(2)
        reason: str | None = None

        if drift_alarm:
            reason = "execution_cost_drift_alarm"
        elif self.ml is None:
            reason = "rules_only_no_model"
        elif self.ml.feature_schema_version != features.schema_version:
            reason = "feature_schema_mismatch"
        elif not self.ml.runtime_approved and not shadow:
            reason = "model_not_runtime_approved"
        elif self.ml.trained_at.tzinfo is None or self.ml.trained_at.utcoffset() is None:
            reason = "model_timestamp_naive"
        elif self.ml.trained_at.astimezone(UTC) > now:
            reason = "model_trained_after_prediction"
        elif now - self.ml.trained_at.astimezone(UTC) > self.max_model_age:
            reason = "model_stale"

        if reason is not None:
            floor = base_floor * (self.drift_floor_multiplier if drift_alarm else Decimal(1))
            return self._prediction(
                schedule_rt=schedule_rt,
                p50=floor,
                p90=floor,
                floor=floor,
                model_id="rules_only",
                fallback=True,
                capital_safe=True,
                reason=reason,
                schedule_calculation=schedule_calculation,
                predicted_symbol=features.symbol,
                expected_holding_seconds=expected_holding_seconds,
            )

        try:
            raw_p50, raw_p90 = self.ml.predict_quantiles(features)  # type: ignore[union-attr]
            p50 = _decimal(raw_p50, name="ml_exec_p50_bps")
            p90 = _decimal(raw_p90, name="ml_exec_p90_bps")
            if p90 < 0 or p90 < p50:
                raise ValueError("invalid execution-cost quantiles")
        except Exception:  # noqa: BLE001 - an ML failure must become a safe fallback
            return self._prediction(
                schedule_rt=schedule_rt,
                p50=base_floor,
                p90=base_floor,
                floor=base_floor,
                model_id="rules_only",
                fallback=True,
                capital_safe=True,
                reason="model_prediction_invalid",
                schedule_calculation=schedule_calculation,
                predicted_symbol=features.symbol,
                expected_holding_seconds=expected_holding_seconds,
            )

        return self._prediction(
            schedule_rt=schedule_rt,
            p50=p50,
            p90=p90,
            floor=base_floor,
            model_id=self.ml.model_id,  # type: ignore[union-attr]
            fallback=False,
            capital_safe=self.ml.runtime_approved,  # type: ignore[union-attr]
            reason=None,
            schedule_calculation=schedule_calculation,
            predicted_symbol=features.symbol,
            expected_holding_seconds=expected_holding_seconds,
        )

    def _prediction(
        self,
        *,
        schedule_rt: Decimal,
        p50: Decimal,
        p90: Decimal,
        floor: Decimal,
        model_id: str,
        fallback: bool,
        capital_safe: bool,
        reason: str | None,
        schedule_calculation: RoundTripFeeCalculation,
        predicted_symbol: str,
        expected_holding_seconds: int | None,
    ) -> FeeModelPrediction:
        used = max(p90, floor)
        return FeeModelPrediction(
            schedule_rt_bps=schedule_rt,
            ml_exec_rt_bps=p50,
            ml_exec_p90_bps=p90,
            exec_floor_rt_bps=floor,
            exec_cost_for_gate_bps=used,
            total_rt_bps=schedule_rt + used,
            model_id=model_id,
            schedule_id=self.schedule.schedule_id,
            schedule_account_verified=self.schedule.account_verified,
            schedule_modifiers_applied=schedule_calculation.applied_modifiers,
            schedule_fallback_reason=schedule_calculation.fallback_reason,
            predicted_symbol=predicted_symbol.upper(),
            expected_holding_seconds=expected_holding_seconds,
            fallback=fallback,
            capital_safe=capital_safe,
            fallback_reason=reason,
        )


@dataclass(frozen=True, slots=True)
class ExecutionFillObservation:
    """One fill label derived from facts known at send and at fill time."""

    features: ExecutionCostFeatures
    mid_at_send: Decimal
    fill_price: Decimal
    realized_exec_bps: Decimal
    schedule_fee_bps: Decimal
    liquidity: Liquidity
    fee_leg: FeeLeg
    hold_seconds: int | None
    close_fee_waived: bool

    @classmethod
    def from_fill(
        cls,
        *,
        features: ExecutionCostFeatures,
        mid_at_send: object,
        fill_price: object,
        schedule: FeeSchedule,
        liquidity: Liquidity,
        fee_leg: FeeLeg = "open",
        hold_seconds: int | None = None,
    ) -> ExecutionFillObservation:
        mid = _decimal(mid_at_send, name="mid_at_send")
        fill = _decimal(fill_price, name="fill_price")
        if mid <= 0 or fill <= 0:
            raise ValueError("mid_at_send and fill_price must be positive")
        direction = Decimal(1) if features.side == "buy" else Decimal(-1)
        realized = direction * (fill - mid) / mid * Decimal(10000)
        fee_calculation = schedule.calculation(
            liquidity,
            leg=fee_leg,
            symbol=features.symbol,
            hold_seconds=hold_seconds,
            as_of=features.observed_at,
        )
        return cls(
            features=features,
            mid_at_send=mid,
            fill_price=fill,
            realized_exec_bps=realized,
            schedule_fee_bps=fee_calculation.all_in_fee_bps,
            liquidity=liquidity,
            fee_leg=fee_leg,
            hold_seconds=hold_seconds,
            close_fee_waived="scalper_close_waiver" in fee_calculation.applied_modifiers,
        )

    def to_ledger_fields(self) -> dict[str, object]:
        return {
            "execution_cost_features": self.features.journal_fields(),
            "mid_at_send": float(self.mid_at_send),
            "fill_price": float(self.fill_price),
            "realized_exec_bps": float(self.realized_exec_bps),
            "schedule_fee_bps": float(self.schedule_fee_bps),
            "liquidity": self.liquidity,
            "fee_leg": self.fee_leg,
            "hold_seconds": self.hold_seconds,
            "close_fee_waived": self.close_fee_waived,
            "execution_label_resolved": True,
        }


def execution_residual_bps(
    observation: ExecutionFillObservation,
    predicted_p50_bps: object,
) -> Decimal:
    """Realized execution residual minus the frozen-at-send P50 prediction."""
    return observation.realized_exec_bps - _decimal(
        predicted_p50_bps, name="predicted_p50_bps"
    )


__all__ = [
    "BINANCEUSDM_REFERENCE",
    "BYBIT_REFERENCE",
    "DELTA_INDIA_REFERENCE",
    "FEATURE_SCHEMA_VERSION",
    "DataQuality",
    "ExecutionCostFeatures",
    "ExecutionCostQuantileModel",
    "ExecutionFillObservation",
    "FeeCalculation",
    "FeeLeg",
    "FeeModelPrediction",
    "FeeSchedule",
    "HybridFeeModel",
    "Liquidity",
    "OrderSide",
    "RoundTripFeeCalculation",
    "ScalperOfferRule",
    "Venue",
    "execution_residual_bps",
]
