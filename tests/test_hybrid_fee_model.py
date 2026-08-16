from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from vnedge.paper.fill_model import FillModel
from vnedge.paper.simulated_exchange import PaperOrderRequest, SimulatedExchange
from vnedge.risk.cost_gate import CostGate, CostProfile
from vnedge.risk.fee_model import (
    DELTA_INDIA_REFERENCE,
    FEATURE_SCHEMA_VERSION,
    ExecutionCostFeatures,
    ExecutionFillObservation,
    FeeSchedule,
    HybridFeeModel,
    ScalperOfferRule,
    execution_residual_bps,
)

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)


def features(*, side: str = "buy", urgency: str = "taker") -> ExecutionCostFeatures:
    return ExecutionCostFeatures(
        observed_at=NOW,
        venue="delta_india",
        symbol="BTCUSDT",
        urgency=urgency,  # type: ignore[arg-type]
        side=side,  # type: ignore[arg-type]
        spread_bps=Decimal("1.5"),
        book_imbalance=Decimal("0.1"),
        atr_1h_bps=Decimal(85),
        volume_rank_24h=Decimal("0.8"),
        hour_utc=12,
        session="eu_us_overlap",
        size_notional_usd=Decimal(250),
        data_quality="ok",
    )


class QuantileModel:
    model_id = "exec-q-v1"
    trained_at = NOW - timedelta(days=1)
    feature_schema_version = FEATURE_SCHEMA_VERSION
    runtime_approved = True

    def __init__(self, p50: object = "3", p90: object = "8") -> None:
        self.values = p50, p90

    def predict_quantiles(self, _features):
        return self.values


def test_scalper_offer_waives_only_eligible_close_leg() -> None:
    schedule = FeeSchedule(
        taker_bps=Decimal(5),
        maker_bps=Decimal(2),
        gst=Decimal("0.18"),
        scalper_offer_active=True,
        scalper_consent=True,
        scalper_rules=(ScalperOfferRule("BTCUSDT", 1800),),
        account_verified=True,
        discounts_verified=True,
        verification_id="delta-statement-2026-08",
        discounts_verified_until=NOW + timedelta(days=1),
        schedule_id="statement-2026-08",
    )

    assert schedule.fee_bps("taker", leg="open") == Decimal("5.90")
    assert schedule.fee_bps(
        "taker", leg="close", symbol="BTCUSDT", hold_seconds=1800
    ) == Decimal(0)
    assert schedule.round_trip_bps(
        "maker", "taker", symbol="BTCUSDT", hold_seconds=1800
    ) == Decimal("2.36")

    too_late = schedule.calculation(
        "taker", leg="close", symbol="BTCUSDT", hold_seconds=1801
    )
    assert too_late.all_in_fee_bps == Decimal("5.90")
    assert too_late.fallback_reason == "scalper_window_exceeded"

    no_consent = replace(schedule, scalper_consent=False).calculation(
        "taker", leg="close", symbol="BTCUSDT", hold_seconds=60
    )
    missing_hold = schedule.calculation("taker", leg="close", symbol="BTCUSDT")
    ineligible = schedule.calculation(
        "taker", leg="close", symbol="SOLUSDT", hold_seconds=60
    )
    assert no_consent.fallback_reason == "scalper_consent_missing"
    assert missing_hold.fallback_reason == "scalper_context_missing"
    assert ineligible.fallback_reason == "scalper_symbol_ineligible"
    assert all(
        row.all_in_fee_bps == Decimal("5.90")
        for row in (no_consent, missing_hold, ineligible)
    )


def test_deto_and_verified_stacking_follow_deterministic_order() -> None:
    deto_only = FeeSchedule(
        taker_bps=Decimal(5),
        maker_bps=Decimal(2),
        gst=Decimal("0.18"),
        deto_discount_active=True,
        account_verified=True,
        discounts_verified=True,
        verification_id="delta-fill-history-1",
        discounts_verified_until=NOW + timedelta(days=1),
        schedule_id="delta-deto-only-v1",
    )
    stacked = FeeSchedule(
        taker_bps=Decimal(5),
        maker_bps=Decimal(2),
        gst=Decimal("0.18"),
        scalper_offer_active=True,
        scalper_consent=True,
        scalper_rules=(ScalperOfferRule("BTCUSDT", 1800),),
        deto_discount_active=True,
        account_verified=True,
        discounts_verified=True,
        discounts_stack_verified=True,
        verification_id="delta-fill-history-2",
        discounts_verified_until=NOW + timedelta(days=1),
        schedule_id="delta-stacked-v1",
    )

    assert deto_only.round_trip_bps("taker", "taker") == Decimal("8.8500")
    stacked_rt = stacked.round_trip_calculation(
        "taker", "taker", symbol="BTCUSDT", hold_seconds=600
    )
    assert stacked_rt.total_bps == Decimal("4.4250")
    assert stacked_rt.entry.applied_modifiers == ("deto",)
    assert stacked_rt.exit.applied_modifiers == ("scalper_close_waiver",)

    one_hour = stacked.round_trip_calculation(
        "taker", "taker", symbol="BTCUSDT", hold_seconds=3600
    )
    assert one_hour.total_bps == Decimal("8.8500")
    assert one_hour.applied_modifiers == ("deto",)
    assert one_hour.exit.fallback_reason == "scalper_window_exceeded"


@pytest.mark.parametrize(
    ("discounts_verified", "stack_verified", "reason"),
    [
        (False, False, "discount_state_unverified"),
        (True, False, "discount_stacking_unverified"),
    ],
)
def test_unknown_discount_or_stacking_state_fails_to_base_plus_gst(
    discounts_verified: bool,
    stack_verified: bool,
    reason: str,
) -> None:
    schedule = FeeSchedule(
        taker_bps=Decimal(5),
        maker_bps=Decimal(2),
        gst=Decimal("0.18"),
        scalper_offer_active=True,
        scalper_consent=True,
        scalper_rules=(ScalperOfferRule("BTCUSDT", 1800),),
        deto_discount_active=True,
        account_verified=True,
        discounts_verified=discounts_verified,
        discounts_stack_verified=stack_verified,
        verification_id="delta-account-check",
        discounts_verified_until=(
            NOW + timedelta(days=1) if discounts_verified else None
        ),
        schedule_id="delta-unconfirmed-stack",
    )

    calculation = schedule.calculation("taker")
    assert calculation.all_in_fee_bps == Decimal("5.90")
    assert calculation.applied_modifiers == ()
    assert calculation.fallback_reason == reason


def test_expired_discount_verification_reverts_to_base_fee() -> None:
    schedule = FeeSchedule(
        taker_bps=Decimal(5),
        maker_bps=Decimal(2),
        gst=Decimal("0.18"),
        deto_discount_active=True,
        account_verified=True,
        discounts_verified=True,
        verification_id="delta-account-snapshot",
        discounts_verified_until=NOW - timedelta(seconds=1),
        schedule_id="delta-expired-deto-state",
    )

    calculation = schedule.calculation("taker", as_of=NOW)
    assert calculation.all_in_fee_bps == Decimal("5.90")
    assert calculation.applied_modifiers == ()
    assert calculation.fallback_reason == "discount_verification_expired"


def test_rules_only_uses_two_leg_floor_and_never_claims_ml() -> None:
    model = HybridFeeModel(
        DELTA_INDIA_REFERENCE,
        floor_slip_bps_per_leg=Decimal(2),
    )

    prediction = model.predict(features(), "taker", "taker")

    assert prediction.schedule_rt_bps == Decimal("11.80")
    assert prediction.exec_cost_for_gate_bps == Decimal(4)
    assert prediction.total_rt_bps == Decimal("15.80")
    assert prediction.fallback and prediction.model_id == "rules_only"
    assert prediction.fallback_reason == "rules_only_no_model"


def test_model_p90_can_raise_but_not_lower_the_execution_floor() -> None:
    high = HybridFeeModel(DELTA_INDIA_REFERENCE, QuantileModel()).predict(
        features(), "taker", "taker"
    )
    low = HybridFeeModel(DELTA_INDIA_REFERENCE, QuantileModel("-1", "1")).predict(
        features(), "taker", "taker"
    )

    assert high.exec_cost_for_gate_bps == Decimal(8)
    assert high.total_rt_bps == high.schedule_rt_bps + Decimal(8)
    assert low.ml_exec_rt_bps == Decimal(-1)
    assert low.exec_cost_for_gate_bps == Decimal(4)


def test_stale_or_drifted_model_fails_to_rules_and_drift_raises_floor() -> None:
    stale = QuantileModel()
    stale.trained_at = NOW - timedelta(days=60)
    hybrid = HybridFeeModel(DELTA_INDIA_REFERENCE, stale, max_model_age=timedelta(days=30))

    stale_prediction = hybrid.predict(features(), "taker", "taker")
    drift_prediction = hybrid.predict(features(), "taker", "taker", drift_alarm=True)

    assert stale_prediction.fallback_reason == "model_stale"
    assert stale_prediction.exec_cost_for_gate_bps == Decimal(4)
    assert drift_prediction.fallback_reason == "execution_cost_drift_alarm"
    assert drift_prediction.exec_cost_for_gate_bps == Decimal("6.0")

    future = QuantileModel()
    future.trained_at = NOW + timedelta(seconds=1)
    future_prediction = HybridFeeModel(DELTA_INDIA_REFERENCE, future).predict(
        features(), "taker", "taker"
    )
    assert future_prediction.fallback_reason == "model_trained_after_prediction"


def test_invalid_quantiles_fail_closed() -> None:
    prediction = HybridFeeModel(
        DELTA_INDIA_REFERENCE,
        QuantileModel("9", "3"),
    ).predict(features(), "taker", "taker")

    assert prediction.fallback
    assert prediction.fallback_reason == "model_prediction_invalid"
    assert prediction.total_rt_bps == Decimal("15.80")


def test_research_model_can_run_in_shadow_but_cannot_bind_cost_gate() -> None:
    research = QuantileModel("10", "20")
    research.runtime_approved = False
    hybrid = HybridFeeModel(DELTA_INDIA_REFERENCE, research)

    live = hybrid.predict(features(), "taker", "taker")
    shadow = hybrid.predict(features(), "taker", "taker", shadow=True)

    assert live.fallback_reason == "model_not_runtime_approved"
    assert live.capital_safe is True and live.model_id == "rules_only"
    assert shadow.fallback is False and shadow.capital_safe is False

    gate = CostGate(CostProfile.SCALP)
    result = gate.evaluate(
        signal_edge_bps=100,
        side="buy",
        urgency="taker",
        expected_holding_seconds=0,
        current_funding_rate=0,
        symbol="BTCUSDT",
        fee_model_prediction=shadow,
    )
    assert result.cost.total_cost_bps == Decimal(14)
    assert result.cost.execution_model_reason == "unapproved_execution_model_ignored"


def test_fill_label_is_sign_adjusted_and_serializable() -> None:
    buy = ExecutionFillObservation.from_fill(
        features=features(side="buy"),
        mid_at_send="100",
        fill_price="100.1",
        schedule=DELTA_INDIA_REFERENCE,
        liquidity="taker",
    )
    sell = ExecutionFillObservation.from_fill(
        features=features(side="sell"),
        mid_at_send="100",
        fill_price="99.9",
        schedule=DELTA_INDIA_REFERENCE,
        liquidity="taker",
    )

    assert buy.realized_exec_bps == Decimal("10.000")
    assert sell.realized_exec_bps == Decimal("10.000")
    assert execution_residual_bps(buy, "7") == Decimal("3.000")
    assert buy.to_ledger_fields()["execution_label_resolved"] is True


def test_fill_label_records_a_verified_scalper_close_waiver() -> None:
    schedule = FeeSchedule(
        taker_bps=Decimal(5),
        maker_bps=Decimal(2),
        gst=Decimal("0.18"),
        scalper_offer_active=True,
        scalper_consent=True,
        scalper_rules=(ScalperOfferRule("BTCUSDT", 1800),),
        account_verified=True,
        discounts_verified=True,
        verification_id="delta-close-fill-proof",
        discounts_verified_until=NOW + timedelta(days=1),
        schedule_id="delta-scalper-close-v1",
    )
    close = ExecutionFillObservation.from_fill(
        features=features(side="sell"),
        mid_at_send=100,
        fill_price=99.9,
        schedule=schedule,
        liquidity="taker",
        fee_leg="close",
        hold_seconds=300,
    )

    assert close.schedule_fee_bps == 0
    assert close.close_fee_waived is True
    assert close.to_ledger_fields()["hold_seconds"] == 300


def test_cost_gate_keeps_its_rules_floor_and_accepts_only_higher_ml_cost() -> None:
    common = {
        "signal_edge_bps": 25,
        "side": "buy",
        "urgency": "taker",
        "expected_holding_seconds": 0,
        "current_funding_rate": 0,
        "symbol": "BTCUSDT",
    }
    gate = CostGate(CostProfile.SCALP, min_net_edge_bps=Decimal(4))
    low = HybridFeeModel(
        FeeSchedule(Decimal(1), Decimal(1), schedule_id="low-test"),
        QuantileModel("0", "1"),
    ).predict(features(), "taker", "taker")
    high = HybridFeeModel(
        DELTA_INDIA_REFERENCE,
        QuantileModel("8", "12"),
    ).predict(features(), "taker", "taker")

    baseline = gate.evaluate(**common)
    guarded_low = gate.evaluate(**common, fee_model_prediction=low)
    guarded_high = gate.evaluate(**common, fee_model_prediction=high)

    assert guarded_low.cost.total_cost_bps == baseline.cost.total_cost_bps
    assert guarded_high.cost.fee_bps == Decimal("11.80")
    assert guarded_high.cost.slippage_bps == Decimal(12)
    assert not guarded_high.approved
    assert guarded_high.cost.execution_model_id == "exec-q-v1"


def test_cost_gate_uses_discount_only_from_account_verified_schedule() -> None:
    verified = FeeSchedule(
        taker_bps=Decimal(5),
        maker_bps=Decimal(2),
        gst=Decimal("0.18"),
        scalper_offer_active=True,
        scalper_consent=True,
        scalper_rules=(ScalperOfferRule("BTCUSDT", 1800),),
        deto_discount_active=True,
        account_verified=True,
        discounts_verified=True,
        discounts_stack_verified=True,
        verification_id="delta-statement-and-fill-check",
        discounts_verified_until=NOW + timedelta(days=1),
        schedule_id="delta-account-verified-v1",
    )
    prediction = HybridFeeModel(verified).predict(
        features(),
        "taker",
        "taker",
        expected_holding_seconds=600,
    )
    result = CostGate(CostProfile.DELTA_SCALP).evaluate(
        signal_edge_bps=100,
        side="buy",
        urgency="taker",
        expected_holding_seconds=600,
        current_funding_rate=0,
        symbol="BTCUSDT",
        fee_model_prediction=prediction,
    )

    assert result.cost.fee_bps == Decimal("4.4250")
    # Delta's existing 6bps RT execution floor remains stricter than the
    # hybrid model's default 4bps floor.
    assert result.cost.slippage_bps == Decimal(6)
    assert result.cost.total_cost_bps == Decimal("10.4250")
    assert result.cost.fee_schedule_account_verified is True
    assert result.cost.fee_modifiers_applied == ("deto", "scalper_close_waiver")

    mismatch = CostGate(CostProfile.DELTA_SCALP).evaluate(
        signal_edge_bps=100,
        side="buy",
        urgency="taker",
        expected_holding_seconds=1801,
        current_funding_rate=0,
        symbol="BTCUSDT",
        fee_model_prediction=prediction,
    )
    assert mismatch.cost.fee_bps == Decimal("11.80")
    assert mismatch.cost.execution_model_reason == "fee_prediction_context_mismatch"


def test_features_reject_naive_time_and_future_unsafe_ranges() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(features(), observed_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="volume_rank_24h"):
        replace(features(), volume_rank_24h=Decimal("1.1"))


def test_fee_schedule_rejects_unaudited_verified_state() -> None:
    with pytest.raises(ValueError, match="verification_id"):
        FeeSchedule(Decimal(5), Decimal(2), account_verified=True)
    with pytest.raises(ValueError, match="discount verification"):
        FeeSchedule(Decimal(5), Decimal(2), discounts_verified=True)


def test_paper_fill_freezes_mid_at_send_and_resolves_execution_label() -> None:
    exchange = SimulatedExchange(FillModel(slippage_bps=2))
    exchange.set_quote("BTCUSDT", 99.9, 100.1)

    exchange.submit_order(
        PaperOrderRequest("paper-1", "BTCUSDT", True, 1.0, order_type="market")
    )
    fill = exchange.get_fills()[0]

    assert fill.mid_at_send == 100.0
    assert fill.liquidity == "taker"
    assert fill.realized_exec_bps == pytest.approx((fill.price - 100.0) * 100)
