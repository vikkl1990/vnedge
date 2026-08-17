"""Causal path-validity policy for research and shadow observation.

This module answers one narrow question for an already-open virtual position:
does the latest *closed* decision bar still support holding toward the next
registered target? It never creates entries, moves targets, overrides a hard
stop, sizes positions, or submits orders. ``ActiveExit`` remains authoritative
for stop, TP, trail, and max-holding decisions and must run before this policy.

The policy is deliberately not wired into ``structure_bos_1h``. That strategy
is frozen; adopting these rules requires a new pre-registered strategy ID and
new out-of-sample evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from vnedge.data.structure import StructureEventType, StructureTrend
from vnedge.data.vwap import DualAVWAPBias
from vnedge.research.avwap_reversal import (
    AVWAPExitAction,
    AVWAPExitObservation,
    evaluate_avwap_reversal_exit,
)

Side = Literal["long", "short"]
DataQuality = Literal["ok", "degraded", "gap"]


class PathAction(str, Enum):
    """Non-executable outcome of one closed-bar path evaluation."""

    HOLD = "hold"
    EXIT_REVERSAL = "exit_reversal"
    EXIT_UNREACHABLE = "exit_unreachable"
    EXIT_UNAVAILABLE = "exit_unavailable"


@dataclass(frozen=True, slots=True)
class PathHoldConfig:
    """Parameters that must be frozen under a future strategy registration."""

    max_target_distance_atr: Decimal
    min_hit_probability: Decimal | None = None
    require_htf_alignment: bool = True
    exit_on_avwap_reversal: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_target_distance_atr, Decimal)
            or not self.max_target_distance_atr.is_finite()
            or self.max_target_distance_atr <= 0
        ):
            raise ValueError("max_target_distance_atr must be finite and positive")
        probability = self.min_hit_probability
        if probability is not None and (
            not isinstance(probability, Decimal)
            or not probability.is_finite()
            or probability < 0
            or probability > 1
        ):
            raise ValueError("min_hit_probability must be inside [0, 1]")


@dataclass(frozen=True, slots=True)
class PathObservation:
    """Closed-bar measurements available at one causal decision boundary."""

    as_of: datetime
    side: Side
    price: Decimal
    next_target: Decimal
    active_stop: Decimal
    atr: Decimal
    htf_trend: StructureTrend
    ltf_trend: StructureTrend
    structure_event: StructureEventType = StructureEventType.NONE
    hit_probability: Decimal | None = None
    data_quality: DataQuality = "ok"
    is_closed: bool = True
    anchor_avwap: Decimal | None = None
    dual_avwap_bias: DualAVWAPBias = "unavailable"


@dataclass(frozen=True, slots=True)
class PathDecision:
    """Measurement artifact; ``continue_hold`` is never order authority."""

    action: PathAction
    continue_hold: bool
    reason: str
    target_distance_atr: Decimal | None
    stop_distance_atr: Decimal | None
    hit_probability: Decimal | None


def evaluate_path_hold(
    observation: PathObservation,
    config: PathHoldConfig,
) -> PathDecision:
    """Evaluate continuation after ``ActiveExit`` returned no decision.

    Decision order is conservative: unusable measurement -> opposing causal
    structure -> lost HTF context -> implausible target distance -> optional
    calibrated probability -> hold. A probability is never invented; when a
    threshold is configured, a missing probability fails closed.
    """

    invalid_reason = _invalid_observation_reason(observation)
    if invalid_reason is not None:
        return _decision(PathAction.EXIT_UNAVAILABLE, invalid_reason, observation)

    side = observation.side
    opposing_trend = (
        StructureTrend.DOWN if side == "long" else StructureTrend.UP
    )
    expected_htf = StructureTrend.UP if side == "long" else StructureTrend.DOWN
    opposing_events = (
        {StructureEventType.BOS_DOWN, StructureEventType.CHOCH_DOWN}
        if side == "long"
        else {StructureEventType.BOS_UP, StructureEventType.CHOCH_UP}
    )

    if observation.structure_event in opposing_events:
        return _decision(
            PathAction.EXIT_REVERSAL,
            f"opposing_{observation.structure_event.value}",
            observation,
        )
    if observation.ltf_trend == opposing_trend:
        return _decision(
            PathAction.EXIT_REVERSAL,
            f"ltf_trend_{opposing_trend.value}",
            observation,
        )
    if observation.htf_trend == opposing_trend:
        return _decision(
            PathAction.EXIT_REVERSAL,
            f"htf_trend_{opposing_trend.value}",
            observation,
        )
    if config.require_htf_alignment and observation.htf_trend != expected_htf:
        return _decision(
            PathAction.EXIT_UNREACHABLE,
            "htf_alignment_lost",
            observation,
        )

    if config.exit_on_avwap_reversal:
        avwap_decision = evaluate_avwap_reversal_exit(
            AVWAPExitObservation(
                as_of=observation.as_of,
                side=observation.side,
                close=observation.price,
                avwap=observation.anchor_avwap,
                dual_avwap_bias=observation.dual_avwap_bias,
                data_quality=observation.data_quality,
                is_closed=observation.is_closed,
            )
        )
        if avwap_decision.action == AVWAPExitAction.UNAVAILABLE:
            return _decision(
                PathAction.EXIT_UNAVAILABLE,
                avwap_decision.reason,
                observation,
            )
        if avwap_decision.action == AVWAPExitAction.EXIT_REVERSAL:
            return _decision(
                PathAction.EXIT_REVERSAL,
                avwap_decision.reason,
                observation,
            )

    target_distance = _directional_distance(
        side,
        observation.price,
        observation.next_target,
    )
    if target_distance <= 0:
        # ActiveExit should have resolved an already-crossed target. Treat a
        # target behind price as a policy/integration fault, never as HOLD.
        return _decision(
            PathAction.EXIT_UNAVAILABLE,
            "target_not_ahead_of_price",
            observation,
        )
    target_distance_atr = target_distance / observation.atr
    if target_distance_atr > config.max_target_distance_atr:
        return _decision(
            PathAction.EXIT_UNREACHABLE,
            "target_beyond_frozen_atr_limit",
            observation,
        )

    threshold = config.min_hit_probability
    probability = observation.hit_probability
    if threshold is not None:
        if probability is None or not probability.is_finite():
            return _decision(
                PathAction.EXIT_UNAVAILABLE,
                "hit_probability_unavailable",
                observation,
            )
        if probability < 0 or probability > 1:
            return _decision(
                PathAction.EXIT_UNAVAILABLE,
                "hit_probability_out_of_range",
                observation,
            )
        if probability < threshold:
            return _decision(
                PathAction.EXIT_UNREACHABLE,
                "hit_probability_below_frozen_threshold",
                observation,
            )

    return _decision(PathAction.HOLD, "path_valid", observation)


def _invalid_observation_reason(observation: PathObservation) -> str | None:
    if observation.as_of.tzinfo is None or observation.as_of.utcoffset() is None:
        return "naive_as_of"
    if not observation.is_closed:
        return "forming_bar"
    if observation.data_quality != "ok":
        return f"data_quality_{observation.data_quality}"
    if observation.side not in {"long", "short"}:
        return "invalid_side"
    if not isinstance(observation.htf_trend, StructureTrend) or not isinstance(
        observation.ltf_trend, StructureTrend
    ):
        return "invalid_structure_trend"
    if not isinstance(observation.structure_event, StructureEventType):
        return "invalid_structure_event"
    probability = observation.hit_probability
    if probability is not None and (
        not isinstance(probability, Decimal)
        or not probability.is_finite()
        or probability < 0
        or probability > 1
    ):
        return "invalid_hit_probability"
    if observation.anchor_avwap is not None and not _positive(
        observation.anchor_avwap
    ):
        return "invalid_anchor_avwap"
    if observation.dual_avwap_bias not in {
        "strong_long",
        "strong_short",
        "between",
        "unavailable",
    }:
        return "invalid_dual_avwap_bias"
    numeric = (
        observation.price,
        observation.next_target,
        observation.active_stop,
        observation.atr,
    )
    if any(not _positive(value) for value in numeric):
        return "invalid_numeric_measurement"
    if observation.side == "long" and observation.active_stop >= observation.price:
        return "invalid_long_stop"
    if observation.side == "short" and observation.active_stop <= observation.price:
        return "invalid_short_stop"
    return None


def _decision(
    action: PathAction,
    reason: str,
    observation: PathObservation,
) -> PathDecision:
    usable = (
        _positive(observation.price)
        and _positive(observation.next_target)
        and _positive(observation.active_stop)
        and _positive(observation.atr)
    )
    target_distance_atr: Decimal | None = None
    stop_distance_atr: Decimal | None = None
    if usable:
        target_distance_atr = (
            abs(observation.next_target - observation.price) / observation.atr
        )
        stop_distance_atr = (
            abs(observation.price - observation.active_stop) / observation.atr
        )
    return PathDecision(
        action=action,
        continue_hold=action == PathAction.HOLD,
        reason=reason,
        target_distance_atr=target_distance_atr,
        stop_distance_atr=stop_distance_atr,
        hit_probability=observation.hit_probability,
    )


def _directional_distance(side: Side, price: Decimal, target: Decimal) -> Decimal:
    return target - price if side == "long" else price - target


def _positive(value: object) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and value > 0
        and math.isfinite(float(value))
    )


__all__ = [
    "PathAction",
    "PathDecision",
    "PathHoldConfig",
    "PathObservation",
    "evaluate_path_hold",
]
