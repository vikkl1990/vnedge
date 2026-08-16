from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from vnedge.ml.drift_supervisor import (
    DRIFT_POLICIES,
    DetectorKind,
    DriftClass,
    DriftObservation,
    DriftPolicy,
    DriftSeverity,
    DriftSupervisor,
    JsonlDriftEventStore,
    build_stream_detector,
    observation_from_execution_fill,
    observations_from_shadow_resolution,
)
from vnedge.ml.river_shadow import ResolvedShadowLabel
from vnedge.risk.fee_model import (
    DELTA_INDIA_REFERENCE,
    ExecutionCostFeatures,
    ExecutionFillObservation,
)

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)


class TriggerDetector:
    def __init__(self, trigger: int = 10) -> None:
        self.trigger = trigger
        self.count = 0
        self.drift_detected = False

    def update(self, value: float) -> None:
        self.count += 1
        self.drift_detected = self.count >= self.trigger


def policy(*, cooldown: timedelta = timedelta(hours=1)) -> DriftPolicy:
    return DriftPolicy(
        stream="test_error",
        detector=DetectorKind.ADWIN,
        drift_class=DriftClass.REAL,
        min_observations=10,
        cooldown=cooldown,
        severity=DriftSeverity.ALARM,
        recommended_action="human review",
        requires_closed_bar=True,
        requires_resolved_outcome=True,
        requires_after_cost=True,
        parameters=(("delta", 0.002),),
    )


def observation(index: int, *, closed: bool = True) -> DriftObservation:
    return DriftObservation(
        stream="test_error",
        value=float(index % 2),
        observed_at=NOW + timedelta(minutes=index),
        source_closed_bar=closed,
        outcome_resolved=True,
        after_cost=True,
    )


def test_supervisor_emits_non_binding_event_after_warmup() -> None:
    configured = policy()
    supervisor = DriftSupervisor(
        policies={configured.stream: configured},
        detector_factory=lambda _: TriggerDetector(),
    )

    for index in range(9):
        assert supervisor.update(observation(index)) is None
    event = supervisor.update(observation(9))

    assert event is not None
    assert event.stream == "test_error"
    assert event.drift_class == "real"
    assert event.severity == "alarm"
    assert event.alert_only is True
    assert event.automatic_action == "none"
    assert event.can_trade is False and event.can_promote is False
    assert event.to_alert()["severity"] == "critical"


def test_cooldown_suppresses_repeated_noise_then_allows_new_event() -> None:
    configured = policy(cooldown=timedelta(minutes=30))
    supervisor = DriftSupervisor(
        policies={configured.stream: configured},
        detector_factory=lambda _: TriggerDetector(),
    )
    for index in range(10):
        first = supervisor.update(observation(index))
    assert first is not None

    assert supervisor.update(observation(10)) is None
    later = DriftObservation(
        "test_error",
        1.0,
        NOW + timedelta(hours=2),
        True,
        True,
        True,
    )
    second = supervisor.update(later)

    assert second is not None and second.event_id != first.event_id
    row = supervisor.status()["streams"][0]
    assert row["alerts"] == 2
    assert row["cooldown_suppressed"] == 1


def test_stream_contract_rejects_forming_or_unresolved_values() -> None:
    configured = policy()
    supervisor = DriftSupervisor(
        policies={configured.stream: configured},
        detector_factory=lambda _: TriggerDetector(),
    )

    with pytest.raises(ValueError, match="closed-bar"):
        supervisor.update(observation(0, closed=False))
    with pytest.raises(ValueError, match="resolved outcome"):
        supervisor.update(DriftObservation("test_error", 0.0, NOW, True, False, True))
    with pytest.raises(ValueError, match="after-cost"):
        supervisor.update(DriftObservation("test_error", 0.0, NOW, True, True, False))


def test_events_persist_in_existing_alert_timeline_shape(tmp_path) -> None:
    configured = policy()
    path = tmp_path / "alerts.jsonl"
    supervisor = DriftSupervisor(
        policies={configured.stream: configured},
        detector_factory=lambda _: TriggerDetector(),
        event_store=JsonlDriftEventStore(path),
    )
    for index in range(10):
        supervisor.update(observation(index))

    payload = json.loads(path.read_text().strip())
    assert payload["rule_id"] == "concept_drift:test_error"
    assert payload["kind"] == "concept_drift"
    assert payload["payload"]["automatic_action"] == "none"
    assert payload["payload"]["can_trade"] is False

    status_path = supervisor.write_status(tmp_path / "drift_status.json")
    status = json.loads(status_path.read_text())
    assert status["summary"]["alerts"] == 1
    assert status["automatic_action"] == "none"
    assert status["can_trade"] is False
    assert not list(tmp_path.glob("*.tmp"))


def test_shadow_resolution_maps_only_resolved_after_cost_streams() -> None:
    resolved = ResolvedShadowLabel(
        signal_id="signal-1",
        resolved_at=NOW,
        probability_before_learning=0.8,
        after_cost_net_bps=-3.0,
        label=False,
        resolved_labels=1,
        running_accuracy=0.0,
        running_brier=0.64,
    )

    streams = observations_from_shadow_resolution(resolved)

    assert [row.stream for row in streams] == [
        "after_cost_error",
        "model_log_loss",
        "paper_net_bps",
    ]
    assert streams[0].value == 1.0
    assert streams[1].value == pytest.approx(-math.log(0.2))
    assert streams[2].value == -3.0
    assert all(row.outcome_resolved and row.after_cost for row in streams)


def test_default_policies_are_frozen_and_separate_drift_classes() -> None:
    assert isinstance(DRIFT_POLICIES, MappingProxyType)
    assert {policy.drift_class for policy in DRIFT_POLICIES.values()} == {
        DriftClass.REAL,
        DriftClass.VIRTUAL,
        DriftClass.COST,
    }
    assert DRIFT_POLICIES["after_cost_error"].parameters == (("delta", 0.002),)
    assert DRIFT_POLICIES["exec_cost_residual_bps"].drift_class == DriftClass.COST
    with pytest.raises(TypeError):
        DRIFT_POLICIES["new"] = policy()  # type: ignore[index]


@pytest.mark.parametrize("stream", ["after_cost_error", "realized_fee_bps", "atr_percentile"])
def test_actual_river_detector_factories_match_registered_policies(stream: str) -> None:
    pytest.importorskip("river")
    detector = build_stream_detector(DRIFT_POLICIES[stream])

    detector.update(0.1)

    assert detector.drift_detected is False


def test_actual_adwin_detects_sustained_after_cost_error_shift() -> None:
    pytest.importorskip("river")
    configured = DRIFT_POLICIES["after_cost_error"]
    supervisor = DriftSupervisor(policies={configured.stream: configured})
    events = []
    for index, value in enumerate([0.0] * 100 + [1.0] * 300):
        event = supervisor.update(
            DriftObservation(
                configured.stream,
                value,
                NOW + timedelta(hours=index),
                True,
                True,
                True,
            )
        )
        if event is not None:
            events.append(event)

    assert len(events) == 1
    assert 100 <= events[0].observations <= 200
    assert events[0].drift_class == "real"


def test_labeled_fill_maps_to_cost_residual_drift_stream() -> None:
    features = ExecutionCostFeatures(
        observed_at=NOW,
        venue="delta_india",
        symbol="BTCUSDT",
        urgency="taker",
        side="buy",
        spread_bps=1,
        atr_1h_bps=50,
        volume_rank_24h="0.5",
        size_notional_usd=100,
        data_quality="ok",
        hour_utc=12,
        session="eu_us",
    )
    fill = ExecutionFillObservation.from_fill(
        features=features,
        mid_at_send=100,
        fill_price=100.1,
        schedule=DELTA_INDIA_REFERENCE,
        liquidity="taker",
    )

    row = observation_from_execution_fill(
        fill,
        predicted_p50_bps=7,
        resolved_at=NOW + timedelta(seconds=1),
    )

    assert row.stream == "exec_cost_residual_bps"
    assert row.value == pytest.approx(3.0)
    assert row.outcome_resolved and row.after_cost
