from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from vnedge.ml.river_shadow import (
    SHADOW_FEATURE_SCHEMA,
    AlertOnlyDriftMonitor,
    RiverShadowConfig,
    RiverShadowMonitor,
    build_adwin_detector,
    build_river_logistic_model,
    build_shadow_features,
    validate_shadow_features,
)

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)


class FakeClassifier:
    def __init__(self, probability: float = 0.8) -> None:
        self.probability = probability
        self.learned: list[tuple[dict[str, float], bool]] = []

    def predict_proba_one(self, features: dict[str, float]) -> dict[bool, float]:
        return {False: 1.0 - self.probability, True: self.probability}

    def learn_one(self, features: dict[str, float], label: bool) -> FakeClassifier:
        self.learned.append((dict(features), label))
        return self


class FakeDriftDetector:
    def __init__(self) -> None:
        self.drift_detected = False

    def update(self, value: float) -> None:
        self.drift_detected = value >= 10.0


def features() -> dict[str, float]:
    return build_shadow_features(
        regime_1h="trending_up",
        regime_4h="trending_up",
        atr_percentile=0.62,
        volume_ratio=1.3,
        room_cost_multiple=2.1,
        break_displacement_atr=0.8,
        dual_avwap_bias="strong_long",
        mtf_aligned=True,
        session_label="us_overlap",
        data_quality="ok",
    )


def test_feature_schema_is_fixed_numeric_and_complete() -> None:
    vector = features()

    assert tuple(vector) == SHADOW_FEATURE_SCHEMA
    assert vector["regime_1h_trending_up"] == 1.0
    assert vector["dual_avwap_strong_long"] == 1.0
    assert vector["session_us_overlap"] == 1.0
    assert vector["data_quality_ok"] == 1.0
    assert validate_shadow_features(vector) == vector

    with pytest.raises(ValueError, match="exactly match"):
        validate_shadow_features({**vector, "new_unregistered_feature": 1.0})


def test_prediction_is_non_binding_and_warmup_cannot_select() -> None:
    model = FakeClassifier(0.9)
    monitor = RiverShadowMonitor(
        model,
        RiverShadowConfig(trial_id="river-shadow-001", min_resolved_labels=30),
    )

    prediction = monitor.observe("signal-1", features(), observed_at=NOW, bar_closed=True)

    assert prediction.probability == 0.9
    assert not prediction.warmup_complete
    assert not prediction.shadow_would_take
    assert prediction.binding is False
    assert prediction.can_trade is False
    assert model.learned == []


def test_forming_bar_observation_is_rejected() -> None:
    monitor = RiverShadowMonitor(
        FakeClassifier(),
        RiverShadowConfig(trial_id="river-shadow-forming", min_resolved_labels=30),
    )

    with pytest.raises(ValueError, match="closed-bar"):
        monitor.observe("forming", features(), observed_at=NOW, bar_closed=False)


def test_model_learns_once_only_after_after_cost_outcome_resolves() -> None:
    model = FakeClassifier(0.8)
    monitor = RiverShadowMonitor(
        model,
        RiverShadowConfig(trial_id="river-shadow-002", min_resolved_labels=30),
    )
    monitor.observe("signal-1", features(), observed_at=NOW, bar_closed=True)

    with pytest.raises(ValueError, match="must be after"):
        monitor.resolve("signal-1", after_cost_net_bps=4.0, resolved_at=NOW)
    assert model.learned == []

    outcome = monitor.resolve(
        "signal-1",
        after_cost_net_bps=-2.5,
        resolved_at=NOW + timedelta(hours=12),
    )

    assert outcome.label is False
    assert outcome.probability_before_learning == 0.8
    assert outcome.after_cost_net_bps == -2.5
    assert outcome.running_accuracy == 0.0
    assert outcome.running_brier == pytest.approx(0.64)
    assert len(model.learned) == 1 and model.learned[0][1] is False
    assert monitor.pending_labels == 0
    with pytest.raises(KeyError, match="already resolved"):
        monitor.resolve(
            "signal-1",
            after_cost_net_bps=1.0,
            resolved_at=NOW + timedelta(hours=13),
        )


def test_shadow_threshold_can_bind_only_to_shadow_after_warmup() -> None:
    model = FakeClassifier(0.8)
    monitor = RiverShadowMonitor(
        model,
        RiverShadowConfig(
            trial_id="river-shadow-003",
            probability_threshold=0.7,
            min_resolved_labels=30,
        ),
    )
    for index in range(30):
        signal_id = f"warmup-{index}"
        opened = NOW + timedelta(hours=index * 2)
        monitor.observe(signal_id, features(), observed_at=opened, bar_closed=True)
        monitor.resolve(
            signal_id,
            after_cost_net_bps=1.0,
            resolved_at=opened + timedelta(hours=1),
        )

    prediction = monitor.observe(
        "post-warmup",
        features(),
        observed_at=NOW + timedelta(hours=61),
        bar_closed=True,
    )

    assert prediction.warmup_complete
    assert prediction.shadow_would_take
    assert prediction.binding is False
    assert prediction.can_trade is False


def test_drift_detector_emits_alert_without_automatic_action() -> None:
    monitor = AlertOnlyDriftMonitor(FakeDriftDetector)

    assert monitor.update("model_error", 1.0, observed_at=NOW) is None
    alert = monitor.update("model_error", 10.0, observed_at=NOW + timedelta(hours=1))

    assert alert is not None
    assert alert.stream == "model_error"
    assert alert.alert_only is True
    assert alert.automatic_action == "none"


def test_snapshot_is_atomic_checksummed_and_explicitly_research_only(tmp_path) -> None:
    monitor = RiverShadowMonitor(
        FakeClassifier(0.6),
        RiverShadowConfig(trial_id="river-shadow-004", min_resolved_labels=30),
    )
    monitor.observe("pending-signal", features(), observed_at=NOW, bar_closed=True)

    model_path, manifest_path = monitor.snapshot(tmp_path, created_at=NOW)

    manifest = json.loads(manifest_path.read_text())
    assert model_path.exists()
    assert manifest["sha256"] == hashlib.sha256(model_path.read_bytes()).hexdigest()
    assert manifest["pending_labels"] == 1
    assert manifest["trial_id"] == "river-shadow-004"
    assert manifest["research_only"] is True
    assert manifest["binding"] is False
    assert manifest["can_trade"] is False
    assert not list(tmp_path.glob("*.tmp"))


def test_optional_river_factories_use_the_one_sample_api() -> None:
    pytest.importorskip("river")
    vector = features()
    model = build_river_logistic_model()

    before = model.predict_proba_one(vector)[True]
    model.learn_one(vector, True)
    after = model.predict_proba_one(vector)[True]
    detector = build_adwin_detector()
    detector.update(0.1)

    assert before == pytest.approx(0.5)
    assert after > before
    assert detector.drift_detected is False
