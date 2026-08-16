from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from vnedge.data.candles import Candle
from vnedge.data.structure import (
    StructureEvent,
    StructureEventType,
    StructureState,
    StructureTrend,
    SwingPairState,
)
from vnedge.data.structure_mtf import (
    Alignment,
    align_structure,
    build_mtf_snapshot,
    fully_closed_htf,
)

_AT = datetime(2026, 8, 1, 12, tzinfo=UTC)
_EMPTY_PAIR = SwingPairState(None, None, None, None)


def _state(trend: StructureTrend) -> StructureState:
    return StructureState(_AT, trend, _EMPTY_PAIR, Decimal(110), Decimal(90), ())


def _event(kind: StructureEventType, prior: StructureTrend) -> StructureEvent:
    return StructureEvent(kind, _AT, Decimal(110), Decimal(111), prior)


def _bar(opened: datetime, timeframe: str) -> Candle:
    hours = 4 if timeframe == "4h" else 1
    return Candle(
        symbol="BTCUSDT",
        timeframe=timeframe,
        open_time=opened,
        close_time=opened + timedelta(hours=hours),
        open=Decimal(100),
        high=Decimal(101),
        low=Decimal(99),
        close=Decimal(100),
        volume=Decimal(1),
        quote_volume=Decimal(100),
        trade_count=1,
    )


def test_align_long_and_short() -> None:
    long, long_reason = align_structure(
        _state(StructureTrend.UP),
        _state(StructureTrend.UP),
        _event(StructureEventType.BOS_UP, StructureTrend.UP),
    )
    short, short_reason = align_structure(
        _state(StructureTrend.DOWN),
        _state(StructureTrend.DOWN),
        _event(StructureEventType.BOS_DOWN, StructureTrend.DOWN),
    )

    assert (long, long_reason) == (Alignment.LONG, "htf_up_ltf_bos_up")
    assert (short, short_reason) == (Alignment.SHORT, "htf_down_ltf_bos_down")


def test_directional_conflict_blocks_countertrend_bos() -> None:
    alignment, reason = align_structure(
        _state(StructureTrend.UP),
        _state(StructureTrend.DOWN),
        _event(StructureEventType.BOS_DOWN, StructureTrend.DOWN),
    )

    assert alignment is Alignment.CONFLICT
    assert reason == "htf_up_ltf_break_down"


def test_htf_range_and_missing_event_are_neutral() -> None:
    ranged = align_structure(
        _state(StructureTrend.RANGE),
        _state(StructureTrend.UP),
        _event(StructureEventType.BOS_UP, StructureTrend.UP),
    )
    no_event = align_structure(
        _state(StructureTrend.UP),
        _state(StructureTrend.UP),
        None,
    )

    assert ranged == (Alignment.NEUTRAL, "htf_range")
    assert no_event == (Alignment.NEUTRAL, "no_ltf_bos")


def test_choch_against_htf_is_conflict_not_entry() -> None:
    alignment, reason = align_structure(
        _state(StructureTrend.DOWN),
        _state(StructureTrend.DOWN),
        _event(StructureEventType.CHOCH_UP, StructureTrend.DOWN),
    )

    assert alignment is Alignment.CONFLICT
    assert reason == "choch_up_vs_htf_down"


def test_fully_closed_htf_excludes_future_and_still_forming_bars() -> None:
    as_of = datetime(2026, 8, 1, 12, tzinfo=UTC)
    visible = _bar(datetime(2026, 8, 1, 8, tzinfo=UTC), "4h")
    closes_later = _bar(datetime(2026, 8, 1, 12, tzinfo=UTC), "4h")
    forming = replace(closes_later, is_closed=False)

    assert fully_closed_htf([visible, closes_later, forming], as_of) == (visible,)


def test_snapshot_is_blocked_for_missing_or_degraded_series() -> None:
    ltf = [_bar(datetime(2026, 8, 1, 11, tzinfo=UTC), "1h")]

    missing = build_mtf_snapshot([], ltf)
    degraded = build_mtf_snapshot([], ltf, data_quality="degraded")

    assert missing.alignment is Alignment.BLOCKED
    assert missing.reason == "missing_series"
    assert degraded.alignment is Alignment.BLOCKED
    assert degraded.reason == "data_quality_degraded"
