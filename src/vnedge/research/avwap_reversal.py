"""Causal AVWAP interaction measurements for research and shadow observation.

The functions in this module consume closed bars only. They classify how price
interacted with an already-causal anchored VWAP and can describe an AVWAP-loss
exit for an existing virtual position. They cannot create entries, submit
orders, alter ``ActiveExit`` state, or grant trade/promotion authority.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from itertools import pairwise
from typing import Literal

from vnedge.data.vwap import DualAVWAPBias

Side = Literal["long", "short"]
DataQuality = Literal["ok", "degraded", "gap"]

_BPS = Decimal(10_000)


class AVWAPInteractionKind(str, Enum):
    """Descriptive closed-bar interaction with one anchored VWAP."""

    UNAVAILABLE = "unavailable"
    NONE = "none"
    BULL_REJECTION = "bull_rejection"
    BEAR_REJECTION = "bear_rejection"
    BULL_RECLAIM = "bull_reclaim"
    BEAR_LOSS = "bear_loss"
    FAILED_BULL_RECLAIM = "failed_bull_reclaim"
    FAILED_BEAR_LOSS = "failed_bear_loss"
    ACCEPTED_ABOVE = "accepted_above"
    ACCEPTED_BELOW = "accepted_below"
    BULL_BAND_REENTRY = "bull_band_reentry"
    BEAR_BAND_REENTRY = "bear_band_reentry"


class AVWAPExitAction(str, Enum):
    """Non-executable AVWAP path decision for an existing position."""

    HOLD = "hold"
    EXIT_REVERSAL = "exit_reversal"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class AVWAPInteractionConfig:
    """Parameters that must be frozen before an OOS comparison."""

    touch_tolerance_bps: Decimal = Decimal(2)
    acceptance_bars: int = 2
    failed_reclaim_window: int = 3

    def __post_init__(self) -> None:
        if not _non_negative(self.touch_tolerance_bps):
            raise ValueError("touch_tolerance_bps must be finite and non-negative")
        if self.acceptance_bars < 2:
            raise ValueError("acceptance_bars must be at least 2")
        if self.failed_reclaim_window < 1:
            raise ValueError("failed_reclaim_window must be positive")


@dataclass(frozen=True, slots=True)
class AVWAPBarObservation:
    """One bar and the causal AVWAP value known at its close."""

    as_of: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    avwap: Decimal | None
    lower_band: Decimal | None = None
    upper_band: Decimal | None = None
    data_quality: DataQuality = "ok"
    is_closed: bool = True


@dataclass(frozen=True, slots=True)
class AVWAPInteraction:
    kind: AVWAPInteractionKind
    reason: str
    as_of: datetime | None
    distance_bps: Decimal | None
    measurement_only: bool = True
    can_trade: bool = False
    can_promote: bool = False


@dataclass(frozen=True, slots=True)
class AVWAPExitObservation:
    """Latest closed-bar state for an already-open virtual position."""

    as_of: datetime
    side: Side
    close: Decimal
    avwap: Decimal | None
    dual_avwap_bias: DualAVWAPBias = "unavailable"
    data_quality: DataQuality = "ok"
    is_closed: bool = True


@dataclass(frozen=True, slots=True)
class AVWAPExitDecision:
    action: AVWAPExitAction
    reason: str
    distance_bps: Decimal | None
    measurement_only: bool = True
    can_trade: bool = False
    can_promote: bool = False


def classify_avwap_interaction(
    observations: Sequence[AVWAPBarObservation],
    config: AVWAPInteractionConfig | None = None,
) -> AVWAPInteraction:
    """Classify the latest interaction using only a contiguous closed-bar suffix."""

    cfg = config or AVWAPInteractionConfig()
    if not observations:
        return _interaction(AVWAPInteractionKind.UNAVAILABLE, "no_observations", None)
    latest = observations[-1]
    reason = _invalid_bar_reason(latest)
    if reason is not None:
        return _interaction(AVWAPInteractionKind.UNAVAILABLE, reason, latest)
    if len(observations) < 2:
        return _interaction(
            AVWAPInteractionKind.UNAVAILABLE,
            "insufficient_history",
            latest,
        )
    if any(
        left.as_of >= right.as_of
        for left, right in pairwise(observations)
    ):
        return _interaction(
            AVWAPInteractionKind.UNAVAILABLE,
            "observations_not_strictly_ordered",
            latest,
        )

    # A gap or forming bar breaks the causal interaction chain. Older history
    # before that boundary is deliberately ignored.
    usable: list[AVWAPBarObservation] = []
    for item in reversed(observations):
        if _invalid_bar_reason(item) is not None:
            break
        usable.append(item)
    usable.reverse()
    if len(usable) < 2:
        return _interaction(
            AVWAPInteractionKind.UNAVAILABLE,
            "no_contiguous_closed_bar_pair",
            latest,
        )

    previous = usable[-2]
    previous_side = _side_of(previous, cfg.touch_tolerance_bps)
    latest_side = _side_of(latest, cfg.touch_tolerance_bps)

    prior = usable[:-1]
    window_start = max(1, len(prior) - cfg.failed_reclaim_window)
    recent_pairs = pairwise(prior[window_start - 1 :])
    reclaimed_above = any(
        _side_of(left, cfg.touch_tolerance_bps) == "below"
        and _side_of(right, cfg.touch_tolerance_bps) == "above"
        for left, right in recent_pairs
    )
    recent_pairs = pairwise(prior[window_start - 1 :])
    lost_below = any(
        _side_of(left, cfg.touch_tolerance_bps) == "above"
        and _side_of(right, cfg.touch_tolerance_bps) == "below"
        for left, right in recent_pairs
    )
    if reclaimed_above and latest_side == "below":
        return _interaction(
            AVWAPInteractionKind.FAILED_BULL_RECLAIM,
            "reclaim_failed_within_frozen_window",
            latest,
        )
    if lost_below and latest_side == "above":
        return _interaction(
            AVWAPInteractionKind.FAILED_BEAR_LOSS,
            "loss_failed_within_frozen_window",
            latest,
        )
    if previous_side == "below" and latest_side == "above":
        return _interaction(
            AVWAPInteractionKind.BULL_RECLAIM,
            "closed_from_below_to_above",
            latest,
        )
    if previous_side == "above" and latest_side == "below":
        return _interaction(
            AVWAPInteractionKind.BEAR_LOSS,
            "closed_from_above_to_below",
            latest,
        )

    assert latest.avwap is not None
    tolerance = latest.avwap * cfg.touch_tolerance_bps / _BPS
    if (
        previous_side == "above"
        and latest.low <= latest.avwap + tolerance
        and latest_side == "above"
    ):
        return _interaction(
            AVWAPInteractionKind.BULL_REJECTION,
            "touched_line_and_closed_back_above",
            latest,
        )
    if (
        previous_side == "below"
        and latest.high >= latest.avwap - tolerance
        and latest_side == "below"
    ):
        return _interaction(
            AVWAPInteractionKind.BEAR_REJECTION,
            "touched_line_and_closed_back_below",
            latest,
        )

    if _bull_band_reentry(previous, latest):
        return _interaction(
            AVWAPInteractionKind.BULL_BAND_REENTRY,
            "closed_back_inside_lower_band",
            latest,
        )
    if _bear_band_reentry(previous, latest):
        return _interaction(
            AVWAPInteractionKind.BEAR_BAND_REENTRY,
            "closed_back_inside_upper_band",
            latest,
        )

    accepted = usable[-cfg.acceptance_bars :]
    if len(accepted) == cfg.acceptance_bars and all(
        _side_of(item, cfg.touch_tolerance_bps) == "above" for item in accepted
    ):
        return _interaction(
            AVWAPInteractionKind.ACCEPTED_ABOVE,
            "frozen_close_count_above",
            latest,
        )
    if len(accepted) == cfg.acceptance_bars and all(
        _side_of(item, cfg.touch_tolerance_bps) == "below" for item in accepted
    ):
        return _interaction(
            AVWAPInteractionKind.ACCEPTED_BELOW,
            "frozen_close_count_below",
            latest,
        )
    return _interaction(AVWAPInteractionKind.NONE, "no_frozen_pattern", latest)


def evaluate_avwap_reversal_exit(
    observation: AVWAPExitObservation,
) -> AVWAPExitDecision:
    """Evaluate AVWAP loss without touching the canonical hard-exit engine."""

    invalid = _invalid_exit_reason(observation)
    if invalid is not None:
        return AVWAPExitDecision(AVWAPExitAction.UNAVAILABLE, invalid, None)
    assert observation.avwap is not None
    distance = (observation.close - observation.avwap) * _BPS / observation.avwap
    if (
        observation.side == "long"
        and observation.close < observation.avwap
        and observation.dual_avwap_bias != "strong_long"
    ):
        return AVWAPExitDecision(
            AVWAPExitAction.EXIT_REVERSAL,
            "long_closed_below_avwap_without_strong_long_bias",
            distance,
        )
    if (
        observation.side == "short"
        and observation.close > observation.avwap
        and observation.dual_avwap_bias != "strong_short"
    ):
        return AVWAPExitDecision(
            AVWAPExitAction.EXIT_REVERSAL,
            "short_closed_above_avwap_without_strong_short_bias",
            distance,
        )
    return AVWAPExitDecision(
        AVWAPExitAction.HOLD,
        "avwap_path_valid",
        distance,
    )


def _side_of(
    observation: AVWAPBarObservation,
    tolerance_bps: Decimal,
) -> Literal["above", "below", "on"]:
    assert observation.avwap is not None
    tolerance = observation.avwap * tolerance_bps / _BPS
    if observation.close > observation.avwap + tolerance:
        return "above"
    if observation.close < observation.avwap - tolerance:
        return "below"
    return "on"


def _bull_band_reentry(
    previous: AVWAPBarObservation,
    latest: AVWAPBarObservation,
) -> bool:
    return (
        previous.lower_band is not None
        and latest.lower_band is not None
        and previous.close < previous.lower_band
        and latest.close >= latest.lower_band
        and latest.avwap is not None
        and latest.close < latest.avwap
    )


def _bear_band_reentry(
    previous: AVWAPBarObservation,
    latest: AVWAPBarObservation,
) -> bool:
    return (
        previous.upper_band is not None
        and latest.upper_band is not None
        and previous.close > previous.upper_band
        and latest.close <= latest.upper_band
        and latest.avwap is not None
        and latest.close > latest.avwap
    )


def _invalid_bar_reason(observation: AVWAPBarObservation) -> str | None:
    if observation.as_of.tzinfo is None or observation.as_of.utcoffset() is None:
        return "naive_as_of"
    if not observation.is_closed:
        return "forming_bar"
    if observation.data_quality != "ok":
        return f"data_quality_{observation.data_quality}"
    if observation.avwap is None or not _positive(observation.avwap):
        return "avwap_unavailable"
    prices = (observation.open, observation.high, observation.low, observation.close)
    if any(not _positive(value) for value in prices):
        return "invalid_price"
    if observation.low > min(observation.open, observation.close, observation.high):
        return "invalid_bar_low"
    if observation.high < max(observation.open, observation.close, observation.low):
        return "invalid_bar_high"
    if observation.lower_band is not None and not _positive(observation.lower_band):
        return "invalid_lower_band"
    if observation.upper_band is not None and not _positive(observation.upper_band):
        return "invalid_upper_band"
    if (
        observation.lower_band is not None
        and observation.upper_band is not None
        and observation.lower_band >= observation.upper_band
    ):
        return "invalid_band_order"
    return None


def _invalid_exit_reason(observation: AVWAPExitObservation) -> str | None:
    if observation.as_of.tzinfo is None or observation.as_of.utcoffset() is None:
        return "naive_as_of"
    if not observation.is_closed:
        return "forming_bar"
    if observation.data_quality != "ok":
        return f"data_quality_{observation.data_quality}"
    if observation.side not in {"long", "short"}:
        return "invalid_side"
    if not _positive(observation.close):
        return "invalid_close"
    if observation.avwap is None or not _positive(observation.avwap):
        return "avwap_unavailable"
    if observation.dual_avwap_bias not in {
        "strong_long",
        "strong_short",
        "between",
        "unavailable",
    }:
        return "invalid_dual_avwap_bias"
    return None


def _interaction(
    kind: AVWAPInteractionKind,
    reason: str,
    observation: AVWAPBarObservation | None,
) -> AVWAPInteraction:
    distance: Decimal | None = None
    if observation is not None and observation.avwap is not None and _positive(
        observation.avwap
    ):
        distance = (
            (observation.close - observation.avwap) * _BPS / observation.avwap
        )
    return AVWAPInteraction(
        kind=kind,
        reason=reason,
        as_of=observation.as_of if observation is not None else None,
        distance_bps=distance,
    )


def _positive(value: object) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and value > 0
        and math.isfinite(float(value))
    )


def _non_negative(value: object) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and value >= 0
        and math.isfinite(float(value))
    )


__all__ = [
    "AVWAPBarObservation",
    "AVWAPExitAction",
    "AVWAPExitDecision",
    "AVWAPExitObservation",
    "AVWAPInteraction",
    "AVWAPInteractionConfig",
    "AVWAPInteractionKind",
    "classify_avwap_interaction",
    "evaluate_avwap_reversal_exit",
]
