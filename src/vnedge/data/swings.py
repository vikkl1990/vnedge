"""Deterministic, causal swing anchors for measurement and research.

This module only identifies structure in closed candles.  It cannot emit a
signal, order intent, capital permission, or promotion decision.  A pivot at
index ``i`` is visible only after the right-side bar ``i + right`` *closes*.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vnedge.data.candles import Candle


class SwingKind(str, Enum):
    LOW = "swing_low"
    HIGH = "swing_high"


@dataclass(frozen=True, slots=True)
class SwingDetectConfig:
    left: int = 3
    right: int = 3
    strict: bool = True

    def __post_init__(self) -> None:
        if self.left < 1 or self.right < 1:
            raise ValueError("swing left and right windows must be >= 1")


DEFAULT_SWING_CONFIG = SwingDetectConfig()
WILLIAMS_FRACTAL_CONFIG = SwingDetectConfig(left=2, right=2, strict=True)


@dataclass(frozen=True, slots=True)
class SwingAnchor:
    kind: SwingKind
    index: int
    anchor_time: datetime
    anchor_price: Decimal
    confirmed_at: datetime
    left: int
    right: int
    strict: bool = True

    @property
    def bar_index(self) -> int:
        """Compatibility alias for the original symmetric implementation."""
        return self.index

    @property
    def price(self) -> Decimal:
        """Compatibility alias for callers that used ``SwingAnchor.price``."""
        return self.anchor_price

    @property
    def length(self) -> int:
        """Compatibility value; asymmetric callers should use left/right."""
        return max(self.left, self.right)

    def visible_at(self, at: datetime) -> bool:
        return _utc(at, label="swing visibility time") >= self.confirmed_at

    def is_confirmed(self, at: datetime) -> bool:
        return self.visible_at(at)


def _utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_bars(candles: Sequence[Candle]) -> None:
    previous_open: datetime | None = None
    symbol: str | None = None
    timeframe: str | None = None
    for candle in candles:
        if not candle.is_closed:
            raise ValueError("swing anchors require closed candles")
        opened = _utc(candle.open_time, label="swing candle open_time")
        closed = _utc(candle.close_time, label="swing candle close_time")
        if candle.open_time.utcoffset() != timedelta(0):
            raise ValueError("swing candles must use UTC timestamps")
        if closed <= opened:
            raise ValueError("swing candle close_time must follow open_time")
        if symbol is None:
            symbol, timeframe = candle.symbol, candle.timeframe
        elif candle.symbol != symbol or candle.timeframe != timeframe:
            raise ValueError("swing candle series must share symbol and timeframe")
        if previous_open is not None and opened <= previous_open:
            raise ValueError("swing candle series must be strictly ordered")
        previous_open = opened


def _eligible_bars(
    candles: Sequence[Candle],
    eligible: Sequence[bool] | None,
) -> tuple[bool, ...]:
    if eligible is None:
        return (True,) * len(candles)
    if len(eligible) != len(candles):
        raise ValueError("swing eligibility must match candle count")
    return tuple(eligible)


def _wins(values: Sequence[Decimal], index: int, *, low: bool, strict: bool) -> bool:
    pivot = values[index]
    if strict:
        return all(
            pivot < value if low else pivot > value
            for offset, value in enumerate(values)
            if offset != index
        )
    extreme = min(values) if low else max(values)
    return pivot == extreme and values.index(extreme) == index


def detect_swings(
    candles: Sequence[Candle],
    config: SwingDetectConfig = DEFAULT_SWING_CONFIG,
    *,
    eligible: Sequence[bool] | None = None,
) -> tuple[SwingAnchor, ...]:
    """Return deterministic anchors whose complete right window is closed.

    ``eligible`` is an explicit data-quality boundary.  A pivot is suppressed
    when any bar in its L/R window is ineligible, so known feed gaps cannot
    manufacture an extreme or bridge otherwise independent structures.  Time
    distance alone is intentionally not treated as a gap because empty crypto
    buckets may represent a healthy quiet market.
    """
    _validate_bars(candles)
    usable = _eligible_bars(candles, eligible)
    if len(candles) < config.left + config.right + 1:
        return ()
    anchors: list[SwingAnchor] = []
    for index in range(config.left, len(candles) - config.right):
        start = index - config.left
        stop = index + config.right + 1
        window = candles[start:stop]
        if not all(usable[start:stop]):
            continue
        pivot_offset = config.left
        confirmed_at = candles[index + config.right].close_time
        if _wins(
            [candle.low for candle in window],
            pivot_offset,
            low=True,
            strict=config.strict,
        ):
            anchors.append(
                SwingAnchor(
                    SwingKind.LOW,
                    index,
                    candles[index].open_time,
                    candles[index].low,
                    confirmed_at,
                    config.left,
                    config.right,
                    config.strict,
                )
            )
        if _wins(
            [candle.high for candle in window],
            pivot_offset,
            low=False,
            strict=config.strict,
        ):
            anchors.append(
                SwingAnchor(
                    SwingKind.HIGH,
                    index,
                    candles[index].open_time,
                    candles[index].high,
                    confirmed_at,
                    config.left,
                    config.right,
                    config.strict,
                )
            )
    return tuple(anchors)


def latest_confirmed_anchors(
    candles: Sequence[Candle],
    *,
    as_of: datetime,
    config: SwingDetectConfig = DEFAULT_SWING_CONFIG,
    eligible: Sequence[bool] | None = None,
) -> tuple[SwingAnchor | None, SwingAnchor | None]:
    """Return the latest low/high pair visible at ``as_of``."""
    visible_at = _utc(as_of, label="swing as_of")
    low: SwingAnchor | None = None
    high: SwingAnchor | None = None
    for anchor in detect_swings(candles, config, eligible=eligible):
        if not anchor.visible_at(visible_at):
            continue
        if anchor.kind == SwingKind.LOW:
            low = anchor
        else:
            high = anchor
    return low, high


def streaming_update(
    candles: Sequence[Candle],
    config: SwingDetectConfig = DEFAULT_SWING_CONFIG,
    *,
    eligible: Sequence[bool] | None = None,
) -> tuple[SwingAnchor, ...]:
    """Return only pivots newly confirmed by the final closed candle."""
    _validate_bars(candles)
    candidate = len(candles) - 1 - config.right
    if candidate < config.left:
        return ()
    return tuple(
        anchor
        for anchor in detect_swings(candles, config, eligible=eligible)
        if anchor.index == candidate
    )


def confirmed_swing_anchors(
    candles: Sequence[Candle],
    *,
    length: int = 3,
) -> tuple[SwingAnchor, ...]:
    """Backward-compatible symmetric strict detector."""
    return detect_swings(
        candles,
        SwingDetectConfig(left=length, right=length, strict=True),
    )
