from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from vnedge.data.candles import Candle
from vnedge.data.swings import (
    WILLIAMS_FRACTAL_CONFIG,
    SwingDetectConfig,
    SwingKind,
    detect_swings,
    latest_confirmed_anchors,
    streaming_update,
)

D = Decimal
START = datetime(2026, 8, 16, tzinfo=UTC)


def bar(index: int, *, high: str, low: str, closed: bool = True) -> Candle:
    opened = START + timedelta(hours=index)
    return Candle(
        symbol="BTCUSDT",
        timeframe="1h",
        open_time=opened,
        close_time=opened + timedelta(hours=1),
        open=D("100"),
        high=D(high),
        low=D(low),
        close=D("100"),
        volume=D("1"),
        quote_volume=D("100"),
        trade_count=1,
        vwap=D("100"),
        is_closed=closed,
    )


def seven_bar_low() -> tuple[Candle, ...]:
    lows = ["10", "9", "8", "7", "8", "9", "10"]
    return tuple(bar(i, high=str(110 + i), low=low) for i, low in enumerate(lows))


def test_asymmetric_swing_confirms_at_right_bar_close() -> None:
    bars = seven_bar_low()[:6]
    anchors = detect_swings(bars, SwingDetectConfig(left=2, right=2))
    low = next(anchor for anchor in anchors if anchor.kind == SwingKind.LOW)

    assert low.index == 3
    assert low.left == 2
    assert low.right == 2
    assert low.anchor_time == bars[3].open_time
    assert low.confirmed_at == bars[5].close_time
    assert not low.visible_at(bars[5].open_time)
    assert low.visible_at(bars[5].close_time)


def test_ties_are_absent_when_strict_and_first_wins_when_non_strict() -> None:
    lows = ["10", "5", "5", "8", "9"]
    bars = tuple(bar(i, high=str(120 + i), low=low) for i, low in enumerate(lows))

    strict = detect_swings(bars, SwingDetectConfig(left=1, right=2, strict=True))
    stable = detect_swings(bars, SwingDetectConfig(left=1, right=2, strict=False))

    assert not any(anchor.kind == SwingKind.LOW for anchor in strict)
    lows_found = [anchor for anchor in stable if anchor.kind == SwingKind.LOW]
    assert [anchor.index for anchor in lows_found] == [1]


def test_latest_anchor_is_hidden_until_confirmation_close() -> None:
    bars = seven_bar_low()
    config = SwingDetectConfig(left=3, right=3)

    before, _ = latest_confirmed_anchors(
        bars,
        as_of=bars[6].open_time,
        config=config,
    )
    after, _ = latest_confirmed_anchors(
        bars,
        as_of=bars[6].close_time,
        config=config,
    )

    assert before is None
    assert after is not None
    assert after.index == 3


def test_streaming_update_returns_only_newly_confirmed_pivot() -> None:
    anchors = streaming_update(seven_bar_low(), SwingDetectConfig(left=3, right=3))
    assert [(anchor.kind, anchor.index) for anchor in anchors] == [
        (SwingKind.LOW, 3)
    ]


def test_forming_bar_is_rejected() -> None:
    bars = seven_bar_low()
    with pytest.raises(ValueError, match="closed"):
        detect_swings((*bars[:-1], replace(bars[-1], is_closed=False)))


def test_williams_fractal_is_the_named_two_by_two_configuration() -> None:
    assert WILLIAMS_FRACTAL_CONFIG == SwingDetectConfig(
        left=2,
        right=2,
        strict=True,
    )


def test_ineligible_bar_suppresses_every_overlapping_pivot_window() -> None:
    bars = seven_bar_low()
    eligible = [True] * len(bars)
    eligible[1] = False

    assert detect_swings(bars, eligible=eligible) == ()


def test_eligibility_must_match_candle_count() -> None:
    with pytest.raises(ValueError, match="match candle count"):
        detect_swings(seven_bar_low(), eligible=[True])


@pytest.mark.parametrize(
    "left,right",
    [(0, 1), (1, 0), (-1, 2)],
)
def test_window_sizes_must_be_positive(left: int, right: int) -> None:
    with pytest.raises(ValueError, match=">= 1"):
        SwingDetectConfig(left=left, right=right)
