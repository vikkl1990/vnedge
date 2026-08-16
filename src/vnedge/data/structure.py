"""Pure, causal HH/HL market-structure measurement.

Only swings whose ``confirmed_at`` is visible at the requested ``as_of`` may
enter the state.  This module describes structure and closed-price events; it
cannot emit a strategy signal, place an order, or grant capital permission.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from vnedge.data.candles import Candle
from vnedge.data.swings import SwingAnchor, SwingDetectConfig, SwingKind, detect_swings


class StructureTrend(str, Enum):
    UP = "up"
    DOWN = "down"
    RANGE = "range"
    NONE = "none"


class StructureEventType(str, Enum):
    BOS_UP = "bos_up"
    BOS_DOWN = "bos_down"
    CHOCH_UP = "choch_up"
    CHOCH_DOWN = "choch_down"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class SwingPairState:
    high_prev: SwingAnchor | None
    high_last: SwingAnchor | None
    low_prev: SwingAnchor | None
    low_last: SwingAnchor | None

    @property
    def is_hh(self) -> bool:
        return (
            self.high_prev is not None
            and self.high_last is not None
            and self.high_last.anchor_price > self.high_prev.anchor_price
        )

    @property
    def is_hl(self) -> bool:
        return (
            self.low_prev is not None
            and self.low_last is not None
            and self.low_last.anchor_price > self.low_prev.anchor_price
        )

    @property
    def is_lh(self) -> bool:
        return (
            self.high_prev is not None
            and self.high_last is not None
            and self.high_last.anchor_price < self.high_prev.anchor_price
        )

    @property
    def is_ll(self) -> bool:
        return (
            self.low_prev is not None
            and self.low_last is not None
            and self.low_last.anchor_price < self.low_prev.anchor_price
        )


@dataclass(frozen=True, slots=True)
class StructureState:
    as_of: datetime
    trend: StructureTrend
    pair: SwingPairState
    last_swing_high: Decimal | None
    last_swing_low: Decimal | None
    labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StructureEvent:
    event: StructureEventType
    ts: datetime
    level: Decimal | None
    close: Decimal | None
    prior_trend: StructureTrend


def _utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _confirmed_swings(
    swings: Sequence[SwingAnchor],
    as_of: datetime,
) -> list[SwingAnchor]:
    visible_at = _utc(as_of, label="structure as_of")
    return [swing for swing in swings if swing.confirmed_at <= visible_at]


def _split_highs_lows(
    swings: Sequence[SwingAnchor],
) -> tuple[list[SwingAnchor], list[SwingAnchor]]:
    highs = [swing for swing in swings if swing.kind == SwingKind.HIGH]
    lows = [swing for swing in swings if swing.kind == SwingKind.LOW]
    highs.sort(key=lambda swing: (swing.anchor_time, swing.index))
    lows.sort(key=lambda swing: (swing.anchor_time, swing.index))
    return highs, lows


def swing_pair_state(
    swings: Sequence[SwingAnchor],
    as_of: datetime,
) -> SwingPairState:
    confirmed = _confirmed_swings(swings, as_of)
    highs, lows = _split_highs_lows(confirmed)
    return SwingPairState(
        high_prev=highs[-2] if len(highs) >= 2 else None,
        high_last=highs[-1] if highs else None,
        low_prev=lows[-2] if len(lows) >= 2 else None,
        low_last=lows[-1] if lows else None,
    )


def classify_hh_hl(pair: SwingPairState) -> StructureTrend:
    """UP=HH+HL, DOWN=LH+LL, mixed/equal=RANGE, incomplete=NONE."""
    if any(
        anchor is None
        for anchor in (pair.high_prev, pair.high_last, pair.low_prev, pair.low_last)
    ):
        return StructureTrend.NONE
    if pair.is_hh and pair.is_hl:
        return StructureTrend.UP
    if pair.is_lh and pair.is_ll:
        return StructureTrend.DOWN
    return StructureTrend.RANGE


def structure_labels(pair: SwingPairState) -> tuple[str, ...]:
    labels: list[str] = []
    if pair.high_prev is not None and pair.high_last is not None:
        labels.append("HH" if pair.is_hh else "LH" if pair.is_lh else "EH")
    if pair.low_prev is not None and pair.low_last is not None:
        labels.append("HL" if pair.is_hl else "LL" if pair.is_ll else "EL")
    return tuple(labels)


def build_structure_state(
    swings: Sequence[SwingAnchor],
    as_of: datetime,
) -> StructureState:
    visible_at = _utc(as_of, label="structure as_of")
    pair = swing_pair_state(swings, visible_at)
    return StructureState(
        as_of=visible_at,
        trend=classify_hh_hl(pair),
        pair=pair,
        last_swing_high=(
            pair.high_last.anchor_price if pair.high_last is not None else None
        ),
        last_swing_low=(
            pair.low_last.anchor_price if pair.low_last is not None else None
        ),
        labels=structure_labels(pair),
    )


def structure_from_bars(
    bars: Sequence[Candle],
    as_of: datetime | None = None,
    config: SwingDetectConfig | None = None,
    *,
    eligible: Sequence[bool] | None = None,
) -> StructureState:
    if not bars:
        raise ValueError("structure requires at least one closed candle")
    cfg = config or SwingDetectConfig(left=3, right=3, strict=True)
    visible_at = as_of or bars[-1].close_time
    return build_structure_state(
        detect_swings(bars, cfg, eligible=eligible),
        visible_at,
    )


def detect_bos_choch(
    state: StructureState,
    close: Decimal,
    break_buffer_bps: Decimal = Decimal(5),
) -> StructureEvent | None:
    """Classify a buffered closed-price break against the prior structure."""
    if not close.is_finite() or close <= 0:
        raise ValueError("structure close must be finite and positive")
    if not break_buffer_bps.is_finite() or break_buffer_bps < 0:
        raise ValueError("break buffer must be finite and non-negative")
    buffer = break_buffer_bps / Decimal(10_000)

    if state.last_swing_high is not None:
        high = state.last_swing_high
        if close > high * (Decimal(1) + buffer):
            if state.trend == StructureTrend.UP:
                return StructureEvent(
                    StructureEventType.BOS_UP,
                    state.as_of,
                    high,
                    close,
                    state.trend,
                )
            if state.trend == StructureTrend.DOWN:
                return StructureEvent(
                    StructureEventType.CHOCH_UP,
                    state.as_of,
                    high,
                    close,
                    state.trend,
                )

    if state.last_swing_low is not None:
        low = state.last_swing_low
        if close < low * (Decimal(1) - buffer):
            if state.trend == StructureTrend.DOWN:
                return StructureEvent(
                    StructureEventType.BOS_DOWN,
                    state.as_of,
                    low,
                    close,
                    state.trend,
                )
            if state.trend == StructureTrend.UP:
                return StructureEvent(
                    StructureEventType.CHOCH_DOWN,
                    state.as_of,
                    low,
                    close,
                    state.trend,
                )
    return None


__all__ = [
    "StructureEvent",
    "StructureEventType",
    "StructureState",
    "StructureTrend",
    "SwingPairState",
    "build_structure_state",
    "classify_hh_hl",
    "detect_bos_choch",
    "structure_from_bars",
    "structure_labels",
    "swing_pair_state",
]
