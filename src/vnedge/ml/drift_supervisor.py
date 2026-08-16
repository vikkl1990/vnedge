"""Pre-registered streaming drift supervision for VNEDGE research shadows.

The supervisor classifies real, virtual, and cost-model drift separately.  It
can emit durable, alert-compatible evidence and recommended operator actions;
it cannot mutate a model, change sizing, block an order, edit a strategy, or
grant/revoke registry permission.  Capital actions remain explicit decisions
in the normal risk and promotion paths.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from vnedge.ml.river_shadow import ResolvedShadowLabel, RiverUnavailable
from vnedge.risk.fee_model import ExecutionFillObservation, execution_residual_bps


class DriftClass(str, Enum):
    REAL = "real"
    VIRTUAL = "virtual"
    COST = "cost"


class DetectorKind(str, Enum):
    ADWIN = "adwin"
    PAGE_HINKLEY = "page_hinkley"
    KSWIN = "kswin"


class DriftSeverity(str, Enum):
    WARNING = "warning"
    ALARM = "alarm"


class StreamDetector(Protocol):
    drift_detected: bool

    def update(self, value: float) -> object: ...


@dataclass(frozen=True, slots=True)
class DriftPolicy:
    stream: str
    detector: DetectorKind
    drift_class: DriftClass
    min_observations: int
    cooldown: timedelta
    severity: DriftSeverity
    recommended_action: str
    requires_closed_bar: bool
    requires_resolved_outcome: bool
    requires_after_cost: bool
    parameters: tuple[tuple[str, float | int], ...]

    def __post_init__(self) -> None:
        if not self.stream.strip() or not self.recommended_action.strip():
            raise ValueError("drift stream and recommended action are required")
        if self.min_observations < 10 or self.cooldown <= timedelta(0):
            raise ValueError("drift policy warmup/cooldown is invalid")
        keys = [key for key, _ in self.parameters]
        if len(keys) != len(set(keys)):
            raise ValueError("drift detector parameters must be unique")

    @property
    def kwargs(self) -> dict[str, float | int]:
        return dict(self.parameters)


DRIFT_POLICIES: Mapping[str, DriftPolicy] = MappingProxyType(
    {
        policy.stream: policy
        for policy in (
            DriftPolicy(
                "after_cost_error",
                DetectorKind.ADWIN,
                DriftClass.REAL,
                30,
                timedelta(hours=24),
                DriftSeverity.ALARM,
                "human review; keep batch rules authoritative",
                True,
                True,
                True,
                (("delta", 0.002),),
            ),
            DriftPolicy(
                "model_log_loss",
                DetectorKind.ADWIN,
                DriftClass.REAL,
                30,
                timedelta(hours=24),
                DriftSeverity.ALARM,
                "freeze shadow updates and review against rules-only baseline",
                True,
                True,
                True,
                (("delta", 0.002),),
            ),
            DriftPolicy(
                "paper_net_bps",
                DetectorKind.PAGE_HINKLEY,
                DriftClass.REAL,
                30,
                timedelta(hours=24),
                DriftSeverity.ALARM,
                "review paper lane for demotion through the normal ladder",
                True,
                True,
                True,
                (
                    ("min_instances", 30),
                    ("delta", 0.005),
                    ("threshold", 50.0),
                    ("alpha", 0.9999),
                ),
            ),
            DriftPolicy(
                "realized_fee_bps",
                DetectorKind.PAGE_HINKLEY,
                DriftClass.COST,
                30,
                timedelta(hours=12),
                DriftSeverity.ALARM,
                "review cost profile and block new risk via operator/risk policy",
                False,
                True,
                True,
                (
                    ("min_instances", 30),
                    ("delta", 0.005),
                    ("threshold", 50.0),
                    ("alpha", 0.9999),
                ),
            ),
            DriftPolicy(
                "exec_cost_residual_bps",
                DetectorKind.ADWIN,
                DriftClass.COST,
                30,
                timedelta(hours=12),
                DriftSeverity.ALARM,
                "fallback to rules-only execution floor and review the cost model",
                False,
                True,
                True,
                (("delta", 0.002),),
            ),
            DriftPolicy(
                "spread_proxy_bps",
                DetectorKind.ADWIN,
                DriftClass.VIRTUAL,
                64,
                timedelta(hours=6),
                DriftSeverity.WARNING,
                "mark feed quality degraded for operator review",
                True,
                False,
                False,
                (("delta", 0.002),),
            ),
            DriftPolicy(
                "atr_percentile",
                DetectorKind.KSWIN,
                DriftClass.VIRTUAL,
                100,
                timedelta(hours=12),
                DriftSeverity.WARNING,
                "add a regime-shift note to Pulse",
                True,
                False,
                False,
                (
                    ("alpha", 0.005),
                    ("window_size", 100),
                    ("stat_size", 30),
                    ("seed", 42),
                ),
            ),
            DriftPolicy(
                "volume_rank",
                DetectorKind.KSWIN,
                DriftClass.VIRTUAL,
                100,
                timedelta(hours=12),
                DriftSeverity.WARNING,
                "add a liquidity-distribution note to Pulse",
                True,
                False,
                False,
                (
                    ("alpha", 0.005),
                    ("window_size", 100),
                    ("stat_size", 30),
                    ("seed", 43),
                ),
            ),
        )
    }
)


@dataclass(frozen=True, slots=True)
class DriftObservation:
    stream: str
    value: float
    observed_at: datetime
    source_closed_bar: bool
    outcome_resolved: bool
    after_cost: bool


@dataclass(frozen=True, slots=True)
class DriftEvent:
    event_id: str
    stream: str
    detector: str
    drift_class: str
    severity: str
    observed_at: datetime
    value: float
    observations: int
    cooldown_seconds: float
    recommended_action: str
    detector_parameters: Mapping[str, float | int]
    alert_only: bool = True
    automatic_action: str = "none"
    can_trade: bool = False
    can_promote: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "stream": self.stream,
            "detector": self.detector,
            "drift_class": self.drift_class,
            "severity": self.severity,
            "observed_at": self.observed_at.isoformat(),
            "value": self.value,
            "observations": self.observations,
            "cooldown_seconds": self.cooldown_seconds,
            "recommended_action": self.recommended_action,
            "detector_parameters": dict(self.detector_parameters),
            "alert_only": self.alert_only,
            "automatic_action": self.automatic_action,
            "can_trade": self.can_trade,
            "can_promote": self.can_promote,
        }

    def to_alert(self) -> dict[str, object]:
        return {
            "ts": self.observed_at.isoformat(),
            "rule_id": f"concept_drift:{self.stream}",
            "severity": "critical" if self.severity == "alarm" else "warning",
            "message": (
                f"{self.drift_class} drift on {self.stream} "
                f"({self.detector}, n={self.observations}); {self.recommended_action}"
            ),
            "kind": "concept_drift",
            "payload": self.to_dict(),
        }


@dataclass(slots=True)
class _StreamState:
    detector: StreamDetector
    observations: int = 0
    alerts: int = 0
    cooldown_suppressed: int = 0
    last_alert_at: datetime | None = None
    last_value: float | None = None


class JsonlDriftEventStore:
    """Append alert-compatible events with a cross-process advisory lock."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(self, event: DriftEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(event.to_alert(), sort_keys=True, separators=(",", ":"))
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def build_stream_detector(policy: DriftPolicy) -> StreamDetector:
    try:
        from river import drift
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RiverUnavailable(
            "DriftSupervisor requires `pip install -e '.[quant-research]'`"
        ) from exc
    kwargs = policy.kwargs
    if policy.detector == DetectorKind.ADWIN:
        return drift.ADWIN(**kwargs)
    if policy.detector == DetectorKind.PAGE_HINKLEY:
        return drift.PageHinkley(**kwargs)
    if policy.detector == DetectorKind.KSWIN:
        return drift.KSWIN(**kwargs)
    raise ValueError(f"unsupported drift detector: {policy.detector}")


class DriftSupervisor:
    """Route typed observations through frozen detectors and cooldowns."""

    def __init__(
        self,
        *,
        policies: Mapping[str, DriftPolicy] = DRIFT_POLICIES,
        detector_factory: Callable[[DriftPolicy], StreamDetector] = build_stream_detector,
        event_store: JsonlDriftEventStore | None = None,
    ) -> None:
        if not policies:
            raise ValueError("DriftSupervisor requires at least one policy")
        self.policies = MappingProxyType(dict(policies))
        self._factory = detector_factory
        self._store = event_store
        self._states: dict[str, _StreamState] = {}

    def _state(self, policy: DriftPolicy) -> _StreamState:
        state = self._states.get(policy.stream)
        if state is None:
            state = _StreamState(self._factory(policy))
            self._states[policy.stream] = state
        return state

    def update(self, observation: DriftObservation) -> DriftEvent | None:
        try:
            policy = self.policies[observation.stream]
        except KeyError as exc:
            raise ValueError(f"unregistered drift stream: {observation.stream}") from exc
        timestamp = observation.observed_at
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("drift observation timestamp must be timezone-aware")
        timestamp = timestamp.astimezone(UTC)
        value = float(observation.value)
        if not math.isfinite(value):
            raise ValueError("drift observation value must be finite")
        if policy.requires_closed_bar and not observation.source_closed_bar:
            raise ValueError(f"{policy.stream} requires a closed-bar source")
        if policy.requires_resolved_outcome and not observation.outcome_resolved:
            raise ValueError(f"{policy.stream} requires a resolved outcome")
        if policy.requires_after_cost and not observation.after_cost:
            raise ValueError(f"{policy.stream} requires an after-cost measurement")

        state = self._state(policy)
        state.detector.update(value)
        state.observations += 1
        state.last_value = value
        if not state.detector.drift_detected or state.observations < policy.min_observations:
            return None
        if state.last_alert_at is not None and timestamp - state.last_alert_at < policy.cooldown:
            state.cooldown_suppressed += 1
            return None

        identity = f"{policy.stream}|{timestamp.isoformat()}|{state.observations}|{value:.12g}"
        event = DriftEvent(
            event_id=sha256(identity.encode()).hexdigest()[:24],
            stream=policy.stream,
            detector=policy.detector.value,
            drift_class=policy.drift_class.value,
            severity=policy.severity.value,
            observed_at=timestamp,
            value=value,
            observations=state.observations,
            cooldown_seconds=policy.cooldown.total_seconds(),
            recommended_action=policy.recommended_action,
            detector_parameters=MappingProxyType(policy.kwargs),
        )
        state.alerts += 1
        state.last_alert_at = timestamp
        if self._store is not None:
            self._store.append(event)
        return event

    def status(self) -> dict[str, object]:
        rows = []
        for stream, policy in self.policies.items():
            state = self._states.get(stream)
            rows.append(
                {
                    "stream": stream,
                    "detector": policy.detector.value,
                    "drift_class": policy.drift_class.value,
                    "min_observations": policy.min_observations,
                    "cooldown_seconds": policy.cooldown.total_seconds(),
                    "observations": state.observations if state else 0,
                    "alerts": state.alerts if state else 0,
                    "cooldown_suppressed": state.cooldown_suppressed if state else 0,
                    "last_alert_at": (
                        state.last_alert_at.isoformat() if state and state.last_alert_at else None
                    ),
                    "last_value": state.last_value if state else None,
                    "recommended_action": policy.recommended_action,
                }
            )
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "streams": rows,
            "summary": {
                "configured_streams": len(rows),
                "observations": sum(int(row["observations"]) for row in rows),
                "alerts": sum(int(row["alerts"]) for row in rows),
            },
            "alert_only": True,
            "automatic_action": "none",
            "can_trade": False,
            "can_promote": False,
        }

    def write_status(self, path: Path | str) -> Path:
        """Atomically publish the read-only status consumed by ops surfaces."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        temporary.write_text(
            json.dumps(self.status(), sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return target


def observations_from_shadow_resolution(
    resolved: ResolvedShadowLabel,
) -> tuple[DriftObservation, ...]:
    """Produce real-drift streams from one delayed, after-cost resolution."""
    probability = min(max(resolved.probability_before_learning, 1e-12), 1 - 1e-12)
    target = float(resolved.label)
    error = float((probability >= 0.5) != resolved.label)
    log_loss = -(target * math.log(probability) + (1 - target) * math.log(1 - probability))
    common = {
        "observed_at": resolved.resolved_at,
        "source_closed_bar": True,
        "outcome_resolved": True,
        "after_cost": True,
    }
    return (
        DriftObservation("after_cost_error", error, **common),
        DriftObservation("model_log_loss", log_loss, **common),
        # Page-Hinkley is configured in both directions. Feed realized net bps
        # directly so sustained degradation or improvement remains observable.
        DriftObservation("paper_net_bps", resolved.after_cost_net_bps, **common),
    )


def observation_from_execution_fill(
    fill: ExecutionFillObservation,
    *,
    predicted_p50_bps: object,
    resolved_at: datetime,
) -> DriftObservation:
    """Build the pre-registered ADWIN residual from one labeled fill.

    The caller must use the P50 prediction frozen at order send, never a model
    prediction recomputed after seeing the fill.
    """
    return DriftObservation(
        stream="exec_cost_residual_bps",
        value=float(execution_residual_bps(fill, predicted_p50_bps)),
        observed_at=resolved_at,
        source_closed_bar=False,
        outcome_resolved=True,
        after_cost=True,
    )


__all__ = [
    "DRIFT_POLICIES",
    "DetectorKind",
    "DriftClass",
    "DriftEvent",
    "DriftObservation",
    "DriftPolicy",
    "DriftSeverity",
    "DriftSupervisor",
    "JsonlDriftEventStore",
    "build_stream_detector",
    "observation_from_execution_fill",
    "observations_from_shadow_resolution",
]
