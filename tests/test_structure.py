from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from vnedge.data.structure import (
    StructureEventType,
    StructureTrend,
    build_structure_state,
    classify_hh_hl,
    detect_bos_choch,
    structure_from_bars,
    structure_labels,
    swing_pair_state,
)
from vnedge.data.swings import SwingAnchor, SwingKind

_START = datetime(2026, 8, 1, tzinfo=UTC)


def _anchor(
    kind: SwingKind,
    index: int,
    price: str,
    *,
    confirmed_index: int | None = None,
) -> SwingAnchor:
    return SwingAnchor(
        kind=kind,
        index=index,
        anchor_time=_START + timedelta(hours=index),
        anchor_price=Decimal(price),
        confirmed_at=_START + timedelta(hours=confirmed_index or index + 4),
        left=3,
        right=3,
        strict=True,
    )


def _swings(
    high_prices: tuple[str, str],
    low_prices: tuple[str, str],
) -> list[SwingAnchor]:
    # Deliberately unsorted: state construction must order each kind by anchor.
    return [
        _anchor(SwingKind.LOW, 18, low_prices[1]),
        _anchor(SwingKind.HIGH, 8, high_prices[0]),
        _anchor(SwingKind.LOW, 4, low_prices[0]),
        _anchor(SwingKind.HIGH, 14, high_prices[1]),
    ]


def test_insufficient_swings_returns_none() -> None:
    swings = [_anchor(SwingKind.HIGH, 8, "110")]
    state = build_structure_state(swings, _START + timedelta(hours=30))

    assert state.trend is StructureTrend.NONE
    assert state.labels == ()
    assert state.last_swing_high == Decimal(110)
    assert state.last_swing_low is None


def test_hh_hl_is_up_and_labels_are_explicit() -> None:
    state = build_structure_state(
        _swings(("110", "115"), ("90", "95")),
        _START + timedelta(hours=30),
    )

    assert state.trend is StructureTrend.UP
    assert state.pair.is_hh and state.pair.is_hl
    assert not state.pair.is_lh and not state.pair.is_ll
    assert state.labels == ("HH", "HL")


def test_lh_ll_is_down() -> None:
    pair = swing_pair_state(
        _swings(("115", "110"), ("95", "90")),
        _START + timedelta(hours=30),
    )

    assert classify_hh_hl(pair) is StructureTrend.DOWN
    assert structure_labels(pair) == ("LH", "LL")


@pytest.mark.parametrize(
    ("highs", "lows", "labels"),
    [
        (("110", "115"), ("95", "90"), ("HH", "LL")),
        (("115", "110"), ("90", "95"), ("LH", "HL")),
        (("110", "110"), ("90", "90"), ("EH", "EL")),
    ],
)
def test_mixed_or_equal_structure_is_range(highs, lows, labels) -> None:
    state = build_structure_state(
        _swings(highs, lows),
        _START + timedelta(hours=30),
    )

    assert state.trend is StructureTrend.RANGE
    assert state.labels == labels


def test_future_confirmation_cannot_change_past_state() -> None:
    swings = _swings(("110", "115"), ("90", "95"))
    as_of = _START + timedelta(hours=30)
    baseline = build_structure_state(swings, as_of)
    swings.extend(
        [
            _anchor(SwingKind.HIGH, 24, "80", confirmed_index=40),
            _anchor(SwingKind.LOW, 25, "70", confirmed_index=40),
        ]
    )

    assert build_structure_state(swings, as_of) == baseline


def test_bos_and_choch_use_prior_trend_and_buffered_closed_price() -> None:
    up = build_structure_state(
        _swings(("110", "115"), ("90", "95")),
        _START + timedelta(hours=30),
    )
    down = build_structure_state(
        _swings(("115", "110"), ("95", "90")),
        _START + timedelta(hours=30),
    )

    assert detect_bos_choch(up, Decimal("115.05")) is None
    bos_up = detect_bos_choch(up, Decimal(116))
    choch_down = detect_bos_choch(up, Decimal(94))
    bos_down = detect_bos_choch(down, Decimal(89))
    choch_up = detect_bos_choch(down, Decimal(111))

    assert bos_up is not None and bos_up.event is StructureEventType.BOS_UP
    assert choch_down is not None and choch_down.event is StructureEventType.CHOCH_DOWN
    assert bos_down is not None and bos_down.event is StructureEventType.BOS_DOWN
    assert choch_up is not None and choch_up.event is StructureEventType.CHOCH_UP


def test_range_break_is_not_labeled_bos_or_choch() -> None:
    state = build_structure_state(
        _swings(("110", "115"), ("95", "90")),
        _START + timedelta(hours=30),
    )

    assert state.trend is StructureTrend.RANGE
    assert detect_bos_choch(state, Decimal(120)) is None


def test_boundaries_reject_naive_time_and_invalid_price() -> None:
    swings = _swings(("110", "115"), ("90", "95"))
    with pytest.raises(ValueError, match="timezone-aware"):
        build_structure_state(swings, _START.replace(tzinfo=None))

    state = build_structure_state(swings, _START + timedelta(hours=30))
    with pytest.raises(ValueError, match="positive"):
        detect_bos_choch(state, Decimal(0))
    with pytest.raises(ValueError, match="non-negative"):
        detect_bos_choch(state, Decimal(100), Decimal(-1))


def test_structure_from_bars_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="at least one"):
        structure_from_bars([])
