"""Pure multi-timeframe structure alignment for measurement and research.

The higher timeframe supplies directional context and the lower timeframe
supplies the closed-price event. Every timeframe confirms its own swings, and
HTF candles are clipped by their actual close time before classification.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from vnedge.data.candles import Candle
from vnedge.data.structure import (
    StructureEvent,
    StructureEventType,
    StructureState,
    StructureTrend,
    build_structure_state,
    detect_bos_choch,
    structure_from_bars,
)
from vnedge.data.swings import SwingAnchor, SwingDetectConfig, SwingKind, streaming_update


class Alignment(str, Enum):
    LONG = "long"
    SHORT = "short"
    CONFLICT = "conflict"
    NEUTRAL = "neutral"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class MTFParams:
    """Frozen defaults. Parameter changes require a new research ID."""

    htf_left: int = 2
    htf_right: int = 2
    ltf_left: int = 3
    ltf_right: int = 3
    break_buffer_bps: Decimal = Decimal(5)
    require_htf_directional: bool = True
    choch_as_conflict: bool = True
    require_ltf_trend_match: bool = True

    def __post_init__(self) -> None:
        if min(self.htf_left, self.htf_right, self.ltf_left, self.ltf_right) < 1:
            raise ValueError("MTF swing windows must be positive")
        if not self.break_buffer_bps.is_finite() or self.break_buffer_bps < 0:
            raise ValueError("MTF break buffer must be finite and non-negative")


MTF_PARAMS = MTFParams()


@dataclass(frozen=True, slots=True)
class MTFStructureSnapshot:
    as_of_ltf: datetime
    htf_tf: str
    ltf_tf: str
    htf: StructureState
    ltf: StructureState
    ltf_event: StructureEvent | None
    alignment: Alignment
    reason: str


class IncrementalStructureState:
    """Causal O(1)-memory swing/structure state for one closed-candle stream.

    Only the L/R confirmation window and the latest two confirmed anchors per
    side are required to reproduce ``structure_from_bars``.  Anchor indexes
    remain absolute even though the working window is bounded.
    """

    def __init__(self, config: SwingDetectConfig) -> None:
        self.config = config
        self._width = config.left + config.right + 1
        self._bars: deque[Candle] = deque(maxlen=self._width)
        self._eligible: deque[bool] = deque(maxlen=self._width)
        self._anchors: dict[SwingKind, deque[SwingAnchor]] = {
            SwingKind.HIGH: deque(maxlen=2),
            SwingKind.LOW: deque(maxlen=2),
        }
        self._seen = 0
        self._symbol: str | None = None
        self._timeframe: str | None = None
        self._last_open: datetime | None = None
        self._last_close: datetime | None = None

    @property
    def bars_seen(self) -> int:
        return self._seen

    @property
    def last_close_time(self) -> datetime | None:
        return self._last_close

    def update(self, candle: Candle, *, eligible: bool = True) -> tuple[SwingAnchor, ...]:
        if not candle.is_closed:
            raise ValueError("incremental structure requires closed candles")
        opened = _utc(candle.open_time, label="incremental candle open_time")
        closed = _utc(candle.close_time, label="incremental candle close_time")
        if closed <= opened:
            raise ValueError("incremental candle close_time must follow open_time")
        if self._symbol is None:
            self._symbol = candle.symbol
            self._timeframe = candle.timeframe
        elif candle.symbol != self._symbol or candle.timeframe != self._timeframe:
            raise ValueError("incremental candle stream changed symbol or timeframe")
        if self._last_open is not None and opened <= self._last_open:
            raise ValueError("incremental candles must be strictly ordered")

        self._bars.append(candle)
        self._eligible.append(bool(eligible))
        self._seen += 1
        self._last_open = opened
        self._last_close = closed
        if len(self._bars) < self._width:
            return ()

        local = streaming_update(
            tuple(self._bars),
            self.config,
            eligible=tuple(self._eligible),
        )
        candidate_index = self._seen - 1 - self.config.right
        confirmed = tuple(replace(anchor, index=candidate_index) for anchor in local)
        for anchor in confirmed:
            self._anchors[anchor.kind].append(anchor)
        return confirmed

    def state(self, as_of: datetime | None = None) -> StructureState:
        visible_at = as_of or self._last_close or datetime.fromtimestamp(0, tz=UTC)
        anchors = sorted(
            (*self._anchors[SwingKind.HIGH], *self._anchors[SwingKind.LOW]),
            key=lambda anchor: (anchor.confirmed_at, anchor.index, anchor.kind.value),
        )
        return build_structure_state(anchors, visible_at)


class IncrementalMTFState:
    """Incremental canonical HTF/LTF alignment with bounded working state."""

    def __init__(
        self,
        *,
        symbol: str,
        htf_tf: str = "4h",
        ltf_tf: str = "1h",
        params: MTFParams | None = None,
    ) -> None:
        self.symbol = symbol
        self.htf_tf = htf_tf
        self.ltf_tf = ltf_tf
        self.params = params or MTF_PARAMS
        self.htf = IncrementalStructureState(
            SwingDetectConfig(
                self.params.htf_left,
                self.params.htf_right,
                strict=True,
            )
        )
        self.ltf = IncrementalStructureState(
            SwingDetectConfig(
                self.params.ltf_left,
                self.params.ltf_right,
                strict=True,
            )
        )

    def on_htf_candle(self, candle: Candle, *, eligible: bool = True) -> None:
        self._validate_stream_candle(candle, self.htf_tf)
        self.htf.update(candle, eligible=eligible)

    def on_ltf_candle(
        self,
        candle: Candle,
        *,
        data_quality: Literal["ok", "degraded", "gap"] = "ok",
    ) -> MTFStructureSnapshot:
        self._validate_stream_candle(candle, self.ltf_tf)
        eligible = data_quality == "ok"
        self.ltf.update(candle, eligible=eligible)
        as_of = _utc(candle.close_time, label="MTF LTF close_time")
        if data_quality != "ok":
            empty = _empty_state(as_of)
            return MTFStructureSnapshot(
                as_of,
                self.htf_tf,
                self.ltf_tf,
                empty,
                empty,
                None,
                Alignment.BLOCKED,
                f"data_quality_{data_quality}",
            )
        if self.htf.bars_seen == 0:
            empty = _empty_state(as_of)
            return MTFStructureSnapshot(
                as_of,
                self.htf_tf,
                self.ltf_tf,
                empty,
                self.ltf.state(as_of),
                None,
                Alignment.BLOCKED,
                "missing_series",
            )
        if self.htf.last_close_time is not None and self.htf.last_close_time > as_of:
            raise ValueError("incremental HTF state contains a future candle")

        htf_state = self.htf.state(as_of)
        ltf_state = self.ltf.state(as_of)
        event = detect_bos_choch(ltf_state, candle.close, self.params.break_buffer_bps)
        alignment, reason = align_structure(htf_state, ltf_state, event, self.params)
        return MTFStructureSnapshot(
            as_of,
            self.htf_tf,
            self.ltf_tf,
            htf_state,
            ltf_state,
            event,
            alignment,
            reason,
        )

    def _validate_stream_candle(self, candle: Candle, timeframe: str) -> None:
        if candle.symbol != self.symbol or candle.timeframe != timeframe:
            raise ValueError("incremental MTF candle symbol/timeframe mismatch")


def _utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _empty_state(as_of: datetime) -> StructureState:
    return build_structure_state([], as_of)


def fully_closed_htf(
    bars: Sequence[Candle],
    as_of_ltf: datetime,
    *,
    htf_tf: str = "4h",
) -> tuple[Candle, ...]:
    """Strict causal clip: include only closed HTF bars closed by LTF decision."""
    visible_at = _utc(as_of_ltf, label="MTF LTF as_of")
    return tuple(
        bar
        for bar in bars
        if bar.is_closed and bar.timeframe == htf_tf and bar.close_time <= visible_at
    )


def align_structure(
    htf: StructureState,
    ltf: StructureState,
    ltf_event: StructureEvent | None,
    params: MTFParams | None = None,
) -> tuple[Alignment, str]:
    """Pure alignment of two causal structure states and one LTF event."""
    p = params or MTF_PARAMS
    if htf.trend == StructureTrend.NONE or ltf.trend == StructureTrend.NONE:
        return Alignment.NEUTRAL, "insufficient_swings"
    if p.require_htf_directional and htf.trend == StructureTrend.RANGE:
        return Alignment.NEUTRAL, "htf_range"
    if ltf_event is None:
        return Alignment.NEUTRAL, "no_ltf_bos"

    event = ltf_event.event
    if event == StructureEventType.BOS_UP:
        if p.require_ltf_trend_match and ltf.trend not in {
            StructureTrend.UP,
            StructureTrend.RANGE,
        }:
            return Alignment.CONFLICT, "ltf_trend_not_up"
        if htf.trend == StructureTrend.UP:
            return Alignment.LONG, "htf_up_ltf_bos_up"
        if htf.trend == StructureTrend.DOWN:
            return Alignment.CONFLICT, "htf_down_ltf_break_up"
        return Alignment.NEUTRAL, "ltf_up_break_no_htf"

    if event == StructureEventType.BOS_DOWN:
        if p.require_ltf_trend_match and ltf.trend not in {
            StructureTrend.DOWN,
            StructureTrend.RANGE,
        }:
            return Alignment.CONFLICT, "ltf_trend_not_down"
        if htf.trend == StructureTrend.DOWN:
            return Alignment.SHORT, "htf_down_ltf_bos_down"
        if htf.trend == StructureTrend.UP:
            return Alignment.CONFLICT, "htf_up_ltf_break_down"
        return Alignment.NEUTRAL, "ltf_down_break_no_htf"

    if p.choch_as_conflict:
        if event == StructureEventType.CHOCH_UP and htf.trend == StructureTrend.DOWN:
            return Alignment.CONFLICT, "choch_up_vs_htf_down"
        if event == StructureEventType.CHOCH_DOWN and htf.trend == StructureTrend.UP:
            return Alignment.CONFLICT, "choch_down_vs_htf_up"
    return Alignment.NEUTRAL, "no_ltf_bos"


def build_mtf_snapshot(
    htf_bars: Sequence[Candle],
    ltf_bars: Sequence[Candle],
    *,
    htf_tf: str = "4h",
    ltf_tf: str = "1h",
    params: MTFParams | None = None,
    data_quality: Literal["ok", "degraded", "gap"] = "ok",
) -> MTFStructureSnapshot:
    """Align structure at the latest fully closed LTF candle."""
    p = params or MTF_PARAMS
    fallback_as_of = datetime.fromtimestamp(0, tz=UTC)
    if ltf_bars:
        fallback_as_of = _utc(ltf_bars[-1].close_time, label="MTF LTF close_time")
    if data_quality != "ok":
        empty = _empty_state(fallback_as_of)
        return MTFStructureSnapshot(
            fallback_as_of,
            htf_tf,
            ltf_tf,
            empty,
            empty,
            None,
            Alignment.BLOCKED,
            f"data_quality_{data_quality}",
        )
    if not htf_bars or not ltf_bars:
        empty = _empty_state(fallback_as_of)
        return MTFStructureSnapshot(
            fallback_as_of,
            htf_tf,
            ltf_tf,
            empty,
            empty,
            None,
            Alignment.BLOCKED,
            "missing_series",
        )

    if any(not bar.is_closed or bar.timeframe != ltf_tf for bar in ltf_bars):
        empty = _empty_state(fallback_as_of)
        return MTFStructureSnapshot(
            fallback_as_of,
            htf_tf,
            ltf_tf,
            empty,
            empty,
            None,
            Alignment.BLOCKED,
            "invalid_ltf_series",
        )
    ltf_symbols = {bar.symbol for bar in ltf_bars}
    htf_symbols = {bar.symbol for bar in htf_bars}
    if len(ltf_symbols) != 1 or htf_symbols != ltf_symbols:
        empty = _empty_state(fallback_as_of)
        return MTFStructureSnapshot(
            fallback_as_of,
            htf_tf,
            ltf_tf,
            empty,
            empty,
            None,
            Alignment.BLOCKED,
            "symbol_mismatch",
        )

    as_of = _utc(ltf_bars[-1].close_time, label="MTF LTF close_time")
    visible_htf = fully_closed_htf(htf_bars, as_of, htf_tf=htf_tf)
    if not visible_htf:
        empty = _empty_state(as_of)
        ltf = structure_from_bars(
            ltf_bars,
            as_of,
            SwingDetectConfig(p.ltf_left, p.ltf_right, strict=True),
        )
        return MTFStructureSnapshot(
            as_of,
            htf_tf,
            ltf_tf,
            empty,
            ltf,
            None,
            Alignment.BLOCKED,
            "no_closed_htf",
        )

    htf = structure_from_bars(
        visible_htf,
        as_of,
        SwingDetectConfig(p.htf_left, p.htf_right, strict=True),
    )
    ltf = structure_from_bars(
        ltf_bars,
        as_of,
        SwingDetectConfig(p.ltf_left, p.ltf_right, strict=True),
    )
    event = detect_bos_choch(ltf, ltf_bars[-1].close, p.break_buffer_bps)
    alignment, reason = align_structure(htf, ltf, event, p)
    return MTFStructureSnapshot(
        as_of,
        htf_tf,
        ltf_tf,
        htf,
        ltf,
        event,
        alignment,
        reason,
    )


__all__ = [
    "MTF_PARAMS",
    "Alignment",
    "IncrementalMTFState",
    "IncrementalStructureState",
    "MTFParams",
    "MTFStructureSnapshot",
    "align_structure",
    "build_mtf_snapshot",
    "fully_closed_htf",
]
