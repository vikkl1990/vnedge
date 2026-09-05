from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from vnedge.execution.evidence import DecisionEnvelope, ExecutionEvidence
from vnedge.execution.idempotency import mint_client_order_id
from vnedge.strategy.arm_evidence import freeze_permission_from_row


def _snapshot(*, close: float = 101.0, source: str = "canonical_tick_lake"):
    return freeze_permission_from_row(
        {
            "timestamp": datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": close,
            "volume": 25.0,
            "quote_volume": 2525.0,
            "trade_count": 40,
            "is_closed": True,
            "data_quality": "ok",
            "candle_source": source,
        },
        decision_timeframe="15m",
        context_timeframes=(),
        allow_long=True,
        allow_short=False,
        reason="test_arm",
    )


def _decision(*, close: float = 101.0, entry_clock: str = "quote_hold"):
    return DecisionEnvelope.create(
        strategy_id="range_expansion_realtime_v2",
        symbol="BTC/USD:USD",
        timeframe="15m",
        side="long",
        permission_snapshot=_snapshot(close=close),
        entry_clock=entry_clock,
    )


def test_arm_identity_is_stable_when_quote_and_venue_attempt_change() -> None:
    arm = _decision()
    accepted_1 = ExecutionEvidence.from_decision(
        arm,
        quote_sequence=100,
        bbo_ts=datetime(2026, 9, 5, 12, 15, 1, tzinfo=UTC),
        quote_age_ms=12.0,
    )
    accepted_2 = ExecutionEvidence.from_decision(
        arm,
        quote_sequence=999,
        bbo_ts=datetime(2026, 9, 5, 12, 15, 4, tzinfo=UTC),
        quote_age_ms=70.0,
    )

    assert accepted_1.decision_id == accepted_2.decision_id == arm.decision_id
    assert mint_client_order_id() != mint_client_order_id()
    assert "quote_sequence" not in arm.as_dict()
    assert "client_order_id" not in arm.as_dict()


def test_bar_content_and_entry_clock_are_part_of_arm_identity() -> None:
    baseline = _decision(close=101.0, entry_clock="quote_hold")
    changed_bar = _decision(close=101.5, entry_clock="quote_hold")
    changed_clock = _decision(close=101.0, entry_clock="next_15m_open")

    assert baseline.decision_id != changed_bar.decision_id
    assert baseline.decision_id != changed_clock.decision_id
    assert baseline.decision_bar_content_hash != changed_bar.decision_bar_content_hash


def test_execution_stage_cannot_rewrite_arm_identity() -> None:
    arm = _decision()
    accepted = ExecutionEvidence.from_decision(arm)

    with pytest.raises(ValueError, match="rewrite ARM identity"):
        replace(accepted, decision_id="dec_000000000000000000000000")

    with pytest.raises(ValueError, match="rewrite ARM identity"):
        replace(accepted, bar_open=accepted.bar_open + timedelta(minutes=15))


def test_decision_envelope_rejects_mismatched_hash() -> None:
    arm = _decision()

    with pytest.raises(ValueError, match="bar hash does not match"):
        replace(arm, decision_bar_content_hash="0" * 64)


def test_exported_arm_envelope_round_trips_with_identity_intact() -> None:
    arm = _decision()

    restored = DecisionEnvelope.from_dict(arm.as_dict())

    assert restored == arm
    assert restored.decision_id == arm.decision_id
