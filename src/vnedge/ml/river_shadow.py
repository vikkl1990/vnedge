"""Optional River online-learning shadow for closed, fee-aware rule outcomes.

This module has no execution adapter and deliberately returns no trade
permission.  It records what a frozen online policy *would* have selected,
learns only after the associated after-cost outcome resolves, and emits drift
alerts without changing strategy state, size, registry eligibility, or capital
permission.  Batch sklearn plus sealed OOS validation remains authoritative.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

SHADOW_FEATURE_SCHEMA = (
    "regime_1h_trending_up",
    "regime_1h_trending_down",
    "regime_1h_high_volatility",
    "regime_1h_low_liquidity",
    "regime_4h_trending_up",
    "regime_4h_trending_down",
    "atr_percentile",
    "volume_ratio",
    "room_cost_multiple",
    "break_displacement_atr",
    "dual_avwap_strong_long",
    "dual_avwap_strong_short",
    "dual_avwap_between",
    "mtf_aligned",
    "session_asia",
    "session_europe",
    "session_us_overlap",
    "session_us",
    "session_off",
    "data_quality_ok",
)


class OnlineClassifier(Protocol):
    def predict_proba_one(self, features: dict[str, float]) -> Mapping[object, float]: ...

    def learn_one(self, features: dict[str, float], label: bool) -> object: ...


class DriftDetector(Protocol):
    drift_detected: bool

    def update(self, value: float) -> object: ...


class RiverUnavailable(RuntimeError):
    """Raised only when an optional River-backed factory is requested."""


def _utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _probability(probabilities: Mapping[object, float]) -> float:
    raw = probabilities.get(True, probabilities.get(1, 0.5))
    result = float(raw)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError("online classifier returned an invalid probability")
    return result


def _one_hot(value: str, expected: str) -> float:
    return 1.0 if value.strip().lower() == expected else 0.0


def build_shadow_features(
    *,
    regime_1h: str,
    regime_4h: str,
    atr_percentile: float,
    volume_ratio: float,
    room_cost_multiple: float,
    break_displacement_atr: float,
    dual_avwap_bias: str,
    mtf_aligned: bool,
    session_label: str,
    data_quality: str,
) -> dict[str, float]:
    """Encode the frozen S1 shadow schema without dynamic feature discovery."""
    numeric = {
        "atr_percentile": atr_percentile,
        "volume_ratio": volume_ratio,
        "room_cost_multiple": room_cost_multiple,
        "break_displacement_atr": break_displacement_atr,
    }
    if any(not math.isfinite(float(value)) for value in numeric.values()):
        raise ValueError("shadow numeric features must be finite")
    features = {
        "regime_1h_trending_up": _one_hot(regime_1h, "trending_up"),
        "regime_1h_trending_down": _one_hot(regime_1h, "trending_down"),
        "regime_1h_high_volatility": _one_hot(regime_1h, "high_volatility"),
        "regime_1h_low_liquidity": _one_hot(regime_1h, "low_liquidity"),
        "regime_4h_trending_up": _one_hot(regime_4h, "trending_up"),
        "regime_4h_trending_down": _one_hot(regime_4h, "trending_down"),
        **{key: float(value) for key, value in numeric.items()},
        "dual_avwap_strong_long": _one_hot(dual_avwap_bias, "strong_long"),
        "dual_avwap_strong_short": _one_hot(dual_avwap_bias, "strong_short"),
        "dual_avwap_between": _one_hot(dual_avwap_bias, "between"),
        "mtf_aligned": float(mtf_aligned),
        "session_asia": _one_hot(session_label, "asia"),
        "session_europe": _one_hot(session_label, "europe"),
        "session_us_overlap": _one_hot(session_label, "us_overlap"),
        "session_us": _one_hot(session_label, "us"),
        "session_off": _one_hot(session_label, "off_session"),
        "data_quality_ok": _one_hot(data_quality, "ok"),
    }
    if tuple(features) != SHADOW_FEATURE_SCHEMA:
        raise RuntimeError("River shadow feature schema drifted")
    return features


def validate_shadow_features(features: Mapping[str, object]) -> dict[str, float]:
    if len(features) != len(SHADOW_FEATURE_SCHEMA) or set(features) != set(SHADOW_FEATURE_SCHEMA):
        raise ValueError("features must exactly match the frozen River shadow schema")
    normalized = {key: float(features[key]) for key in SHADOW_FEATURE_SCHEMA}
    if any(not math.isfinite(value) for value in normalized.values()):
        raise ValueError("shadow features must be finite")
    return normalized


@dataclass(frozen=True, slots=True)
class RiverShadowConfig:
    trial_id: str
    model_version: str = "river_logistic_shadow_v1"
    probability_threshold: float = 0.55
    min_resolved_labels: int = 200

    def __post_init__(self) -> None:
        if not self.trial_id.strip():
            raise ValueError("trial_id is required for hypothesis accounting")
        if not self.model_version.strip():
            raise ValueError("model_version is required")
        if not 0.5 <= self.probability_threshold < 1.0:
            raise ValueError("probability_threshold must be in [0.5, 1)")
        if self.min_resolved_labels < 30:
            raise ValueError("online shadow requires at least 30 resolved labels")


@dataclass(frozen=True, slots=True)
class ShadowPrediction:
    signal_id: str
    observed_at: datetime
    probability: float
    shadow_would_take: bool
    warmup_complete: bool
    resolved_labels: int
    model_version: str
    trial_id: str
    binding: bool = False
    can_trade: bool = False
    reason: str = "research shadow only"


@dataclass(frozen=True, slots=True)
class ResolvedShadowLabel:
    signal_id: str
    resolved_at: datetime
    probability_before_learning: float
    after_cost_net_bps: float
    label: bool
    resolved_labels: int
    running_accuracy: float
    running_brier: float
    binding: bool = False
    can_trade: bool = False


@dataclass(frozen=True, slots=True)
class DriftAlert:
    stream: str
    observed_at: datetime
    value: float
    alert_only: bool = True
    automatic_action: str = "none"
    message: str = "concept drift observed; human review required"


@dataclass(slots=True)
class _PendingObservation:
    observed_at: datetime
    features: dict[str, float]
    probability: float


class RiverShadowMonitor:
    """Prequential, delayed-label monitor that cannot affect eligibility."""

    def __init__(self, model: OnlineClassifier, config: RiverShadowConfig) -> None:
        self.model = model
        self.config = config
        self._pending: dict[str, _PendingObservation] = {}
        self._resolved = 0
        self._correct = 0
        self._brier_sum = 0.0

    @property
    def resolved_labels(self) -> int:
        return self._resolved

    @property
    def pending_labels(self) -> int:
        return len(self._pending)

    def observe(
        self,
        signal_id: str,
        features: Mapping[str, object],
        *,
        observed_at: datetime,
        bar_closed: bool,
    ) -> ShadowPrediction:
        if not signal_id.strip():
            raise ValueError("signal_id is required")
        if signal_id in self._pending:
            raise ValueError("signal_id already has an unresolved shadow observation")
        if not bar_closed:
            raise ValueError("River shadow accepts closed-bar observations only")
        timestamp = _utc(observed_at, "observed_at")
        normalized = validate_shadow_features(features)
        probability = _probability(self.model.predict_proba_one(normalized))
        warm = self._resolved >= self.config.min_resolved_labels
        self._pending[signal_id] = _PendingObservation(timestamp, normalized, probability)
        return ShadowPrediction(
            signal_id=signal_id,
            observed_at=timestamp,
            probability=probability,
            shadow_would_take=warm and probability >= self.config.probability_threshold,
            warmup_complete=warm,
            resolved_labels=self._resolved,
            model_version=self.config.model_version,
            trial_id=self.config.trial_id,
        )

    def resolve(
        self,
        signal_id: str,
        *,
        after_cost_net_bps: float,
        resolved_at: datetime,
    ) -> ResolvedShadowLabel:
        pending = self._pending.get(signal_id)
        if pending is None:
            raise KeyError(f"unknown or already resolved signal_id: {signal_id}")
        timestamp = _utc(resolved_at, "resolved_at")
        if timestamp <= pending.observed_at:
            raise ValueError("resolved_at must be after the signal observation")
        net = float(after_cost_net_bps)
        if not math.isfinite(net):
            raise ValueError("after_cost_net_bps must be finite")

        label = net > 0.0
        predicted = pending.probability >= 0.5
        # Supervised state changes only after the fee-aware outcome exists.
        self.model.learn_one(pending.features, label)
        self._resolved += 1
        self._correct += int(predicted == label)
        self._brier_sum += (pending.probability - float(label)) ** 2
        del self._pending[signal_id]
        return ResolvedShadowLabel(
            signal_id=signal_id,
            resolved_at=timestamp,
            probability_before_learning=pending.probability,
            after_cost_net_bps=net,
            label=label,
            resolved_labels=self._resolved,
            running_accuracy=self._correct / self._resolved,
            running_brier=self._brier_sum / self._resolved,
        )

    def snapshot(self, directory: Path | str, *, created_at: datetime) -> tuple[Path, Path]:
        """Atomically persist model/state plus a checksum manifest; never loads pickle."""
        timestamp = _utc(created_at, "created_at")
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        stem = f"{self.config.model_version}-{self._resolved:08d}"
        model_path = root / f"{stem}.pkl"
        manifest_path = root / f"{stem}.json"
        payload = pickle.dumps(
            {
                "model": self.model,
                "config": self.config,
                "pending": self._pending,
                "resolved": self._resolved,
                "correct": self._correct,
                "brier_sum": self._brier_sum,
                "feature_schema": SHADOW_FEATURE_SCHEMA,
            },
            protocol=pickle.HIGHEST_PROTOCOL,
        )
        digest = hashlib.sha256(payload).hexdigest()
        manifest = {
            "created_at": timestamp.isoformat(),
            "trial_id": self.config.trial_id,
            "model_version": self.config.model_version,
            "resolved_labels": self._resolved,
            "pending_labels": len(self._pending),
            "running_accuracy": self._correct / self._resolved if self._resolved else None,
            "running_brier": self._brier_sum / self._resolved if self._resolved else None,
            "probability_threshold": self.config.probability_threshold,
            "min_resolved_labels": self.config.min_resolved_labels,
            "feature_schema": SHADOW_FEATURE_SCHEMA,
            "sha256": digest,
            "research_only": True,
            "binding": False,
            "can_trade": False,
            "trial_policy": "each model/update policy counts in raw N and N_eff evidence",
        }
        model_tmp = model_path.with_suffix(".pkl.tmp")
        manifest_tmp = manifest_path.with_suffix(".json.tmp")
        model_tmp.write_bytes(payload)
        manifest_tmp.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
        )
        os.replace(model_tmp, model_path)
        os.replace(manifest_tmp, manifest_path)
        return model_path, manifest_path


class AlertOnlyDriftMonitor:
    def __init__(self, detector_factory: Callable[[], DriftDetector]) -> None:
        self._factory = detector_factory
        self._detectors: dict[str, DriftDetector] = {}

    def update(
        self,
        stream: str,
        value: float,
        *,
        observed_at: datetime,
    ) -> DriftAlert | None:
        if not stream.strip():
            raise ValueError("drift stream name is required")
        measurement = float(value)
        if not math.isfinite(measurement):
            raise ValueError("drift measurement must be finite")
        detector = self._detectors.setdefault(stream, self._factory())
        detector.update(measurement)
        if not detector.drift_detected:
            return None
        return DriftAlert(stream, _utc(observed_at, "observed_at"), measurement)


def build_river_logistic_model() -> OnlineClassifier:
    """Construct the optional frozen baseline without making River a live dependency."""
    try:
        from river import linear_model, preprocessing
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RiverUnavailable(
            "River shadow requires `pip install -e '.[quant-research]'`"
        ) from exc
    return preprocessing.StandardScaler() | linear_model.LogisticRegression(l2=0.001)


def build_adwin_detector(*, delta: float = 0.002) -> DriftDetector:
    try:
        from river import drift
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RiverUnavailable(
            "ADWIN monitoring requires `pip install -e '.[quant-research]'`"
        ) from exc
    return drift.ADWIN(delta=delta)


__all__ = [
    "SHADOW_FEATURE_SCHEMA",
    "AlertOnlyDriftMonitor",
    "DriftAlert",
    "ResolvedShadowLabel",
    "RiverShadowConfig",
    "RiverShadowMonitor",
    "RiverUnavailable",
    "ShadowPrediction",
    "build_adwin_detector",
    "build_river_logistic_model",
    "build_shadow_features",
    "validate_shadow_features",
]
