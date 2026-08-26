from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from vnedge.data.candles import Candle, aggregate_candle_series
from vnedge.data.structure import (
    StructureEvent,
    StructureEventType,
    StructureState,
    StructureTrend,
    SwingPairState,
)
from vnedge.data.structure_mtf import (
    Alignment,
    IncrementalMTFState,
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


def test_incremental_mtf_is_bit_exact_to_full_recompute_for_90_days() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    ltf: list[Candle] = []
    for index in range(90 * 24):
        opened = start + timedelta(hours=index)
        # Deterministic multi-scale wave: enough alternating pivots to exercise
        # HH/HL, LH/LL, range, BoS, and CHoCH classifications.
        cycle = Decimal((index % 37) - 18) / Decimal(8)
        drift = Decimal(index) / Decimal(250)
        close = Decimal(100) + drift + cycle
        ltf.append(
            Candle(
                symbol="BTCUSDT",
                timeframe="1h",
                open_time=opened,
                close_time=opened + timedelta(hours=1),
                open=close - Decimal("0.2"),
                high=close + Decimal("0.8") + Decimal(index % 3) / Decimal(10),
                low=close - Decimal("0.8") - Decimal(index % 5) / Decimal(10),
                close=close,
                volume=Decimal(10 + index % 7),
                quote_volume=close * Decimal(10 + index % 7),
                trade_count=10 + index % 11,
            )
        )
    htf = list(aggregate_candle_series("BTCUSDT", "1h", "4h", ltf))
    incremental = IncrementalMTFState(symbol="BTCUSDT")
    htf_position = 0

    for position, bar in enumerate(ltf):
        while htf_position < len(htf) and htf[htf_position].close_time <= bar.close_time:
            incremental.on_htf_candle(htf[htf_position])
            htf_position += 1
        actual = incremental.on_ltf_candle(bar)
        expected = build_mtf_snapshot(htf[:htf_position], ltf[: position + 1])

        assert actual.alignment == expected.alignment
        assert actual.reason == expected.reason
        assert actual.htf.trend == expected.htf.trend
        assert actual.htf.labels == expected.htf.labels
        assert actual.htf.last_swing_high == expected.htf.last_swing_high
        assert actual.htf.last_swing_low == expected.htf.last_swing_low
        assert actual.ltf.trend == expected.ltf.trend
        assert actual.ltf.labels == expected.ltf.labels
        assert actual.ltf.last_swing_high == expected.ltf.last_swing_high
        assert actual.ltf.last_swing_low == expected.ltf.last_swing_low
        assert actual.ltf_event == expected.ltf_event
