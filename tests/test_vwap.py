from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from vnedge.data.candles import Candle
from vnedge.data.vwap import (
    AnchoredVWAP,
    CandleVWAPAccumulator,
    RunningVWAP,
    SessionVWAP,
    anchored_vwap_series,
    confirmed_swing_anchors,
    dual_avwap_bias,
    price_vs_vwap_bps,
    quantize_to_tick,
    vwap_from_sums,
    vwap_from_trades,
    vwap_merge,
)

D = Decimal
START = datetime(2026, 8, 16, tzinfo=UTC)


def candle(
    index: int,
    *,
    low: str = "99",
    high: str = "101",
    volume: str = "1",
    quote_volume: str = "100",
) -> Candle:
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
        volume=D(volume),
        quote_volume=D(quote_volume),
        trade_count=1,
        vwap=D(quote_volume) / D(volume),
    )


def test_vwap_from_sums_and_trades() -> None:
    trades = [(D("100"), D("2")), (D("102"), D("2"))]
    assert vwap_from_trades(trades) == D("101")
    assert vwap_from_sums(D("404"), D("4")) == D("101")


@pytest.mark.parametrize(
    ("quote", "base"),
    [("0", "1"), ("1", "0"), ("-1", "1"), ("NaN", "1"), ("1", "Infinity")],
)
def test_vwap_from_sums_fails_closed_for_invalid_values(quote: str, base: str) -> None:
    assert vwap_from_sums(D(quote), D(base)) is None


def test_vwap_from_trades_skips_bad_ticks() -> None:
    trades = [
        (D("100"), D("2")),
        (D("0"), D("50")),
        (D("999"), D("0")),
        (D("-1"), D("2")),
        (D("NaN"), D("2")),
    ]
    assert vwap_from_trades(trades) == D("100")
    assert vwap_from_trades(trades[1:]) is None


def test_vwap_merge_weights_child_volume_instead_of_averaging_vwaps() -> None:
    # 10 @ 100 and 1 @ 110 is 1010/11, not the unweighted mean of 105.
    result = vwap_merge([D("1000"), D("110")], [D("10"), D("1")])
    assert result == D("1110") / D("11")
    assert result != D("105")
    with pytest.raises(ValueError, match="equal lengths"):
        vwap_merge([D("1000")], [D("10"), D("1")])
    assert vwap_merge([D("1000"), D("0")], [D("10"), D("0")]) == D("100")
    assert vwap_merge([D("1000"), D("1")], [D("10"), D("0")]) is None


def test_running_vwap_update_skip_and_reset() -> None:
    running = RunningVWAP()
    assert running.update(D("10"), D("1")) == D("10")
    assert running.update(D("20"), D("1")) == D("15")
    assert running.update(D("999"), D("0")) == D("15")
    assert running.trade_count == 2
    assert running.quote_volume == D("30")
    assert running.base_volume == D("2")
    running.reset()
    assert running.value is None
    assert running.trade_count == 0


def test_running_vwap_accepts_exact_child_sums() -> None:
    running = RunningVWAP()
    running.update_sums(D("1000"), D("10"), trade_count=5)
    running.update_sums(D("110"), D("1"), trade_count=1)
    assert running.value == D("1110") / D("11")
    assert running.trade_count == 6
    assert running.update_sums(D("0"), D("0")) == running.value
    with pytest.raises(ValueError, match="trade_count"):
        running.update_sums(D("100"), D("1"), trade_count=-1)


def test_candle_vwap_accumulator_exposes_window_sums() -> None:
    accumulator = CandleVWAPAccumulator("BTCUSDT", "1m", START)
    accumulator.on_trade(D("100"), D("3"))
    accumulator.on_trade(D("110"), D("1"))
    assert accumulator.vwap == D("410") / D("4")
    assert accumulator.volume == D("4")
    assert accumulator.quote_volume == D("410")
    assert accumulator.trade_count == 2


def test_session_vwap_resets_on_custom_utc_boundary() -> None:
    session = SessionVWAP(session_start_hour_utc=12)
    session.on_trade(START + timedelta(hours=11), D("100"), D("2"))
    assert session.vwap == D("100")
    # Exactly 12:00 UTC starts a fresh anchored session.
    session.on_trade(START + timedelta(hours=12), D("110"), D("1"))
    assert session.vwap == D("110")
    session.on_trade(START + timedelta(hours=13), D("120"), D("1"))
    assert session.vwap == D("115")
    assert session.trade_count == 2


def test_session_vwap_normalizes_timezone_and_rejects_bad_time_order() -> None:
    session = SessionVWAP()
    india = timezone(timedelta(hours=5, minutes=30))
    session.on_trade(datetime(2026, 8, 16, 5, 30, tzinfo=india), D("100"), D("1"))
    assert session.vwap == D("100")
    with pytest.raises(ValueError, match="ordered"):
        session.on_trade(START - timedelta(seconds=1), D("101"), D("1"))
    with pytest.raises(ValueError, match="timezone-aware"):
        SessionVWAP().on_trade(datetime(2026, 8, 16), D("100"), D("1"))  # noqa: DTZ001


def test_session_start_hour_is_validated() -> None:
    with pytest.raises(ValueError, match=r"\[0, 23\]"):
        SessionVWAP(session_start_hour_utc=24)


def test_price_distance_and_tick_quantization() -> None:
    assert price_vs_vwap_bps(D("101"), D("100")) == D("100")
    assert price_vs_vwap_bps(D("99"), D("100")) == D("-100")
    assert price_vs_vwap_bps(D("100"), None) is None
    assert price_vs_vwap_bps(D("0"), D("100")) is None
    assert quantize_to_tick(D("100.125"), D("0.05")) == D("100.15")
    assert quantize_to_tick(D("100"), D("0")) is None


def test_anchored_vwap_ignores_pre_anchor_trades_and_can_reanchor() -> None:
    avwap = AnchoredVWAP(START + timedelta(hours=1), anchor_label="breakout")
    assert avwap.on_trade(START, D("90"), D("50")) is None
    assert avwap.on_trade(START + timedelta(hours=1), D("100"), D("10")) == D("100")
    assert avwap.on_trade(START + timedelta(hours=2), D("110"), D("1")) == D("1110") / D("11")
    assert avwap.volume == D("11")
    assert avwap.trade_count == 2

    avwap.reanchor(START + timedelta(hours=3), label="event")
    assert avwap.value is None
    assert avwap.anchor_label == "event"
    assert avwap.on_trade(START + timedelta(hours=3), D("120"), D("2")) == D("120")


def test_anchored_vwap_bar_mode_uses_quote_base_and_never_mixes_inputs() -> None:
    first = candle(0, volume="10", quote_volume="1000")
    second = candle(1, high="110", volume="1", quote_volume="110")
    avwap = AnchoredVWAP.from_bar(first, label="swing_low")
    assert avwap.on_candle(first) == D("100")
    assert avwap.on_candle(second) == D("1110") / D("11")
    assert avwap.trade_count == 2
    with pytest.raises(ValueError, match="cannot mix"):
        avwap.on_trade(START + timedelta(hours=3), D("120"), D("1"))


def test_anchored_vwap_timestamp_inside_bar_starts_at_next_complete_bar() -> None:
    bars = (
        candle(0, volume="10", quote_volume="1000"),
        candle(1, high="110", volume="1", quote_volume="110"),
    )
    series = anchored_vwap_series(bars, START + timedelta(minutes=30))
    assert series == (None, D("110"))
    assert anchored_vwap_series(bars, 0) == (D("100"), D("1110") / D("11"))
    assert anchored_vwap_series(bars, 1) == (None, D("110"))
    with pytest.raises(IndexError, match="outside"):
        anchored_vwap_series(bars, 2)


def test_anchored_vwap_rejects_forming_or_reordered_bars() -> None:
    avwap = AnchoredVWAP(START)
    first = candle(0)
    with pytest.raises(ValueError, match="closed"):
        avwap.on_candle(replace(first, is_closed=False))
    avwap.on_candle(first)
    with pytest.raises(ValueError, match="ordered"):
        avwap.on_candle(first)


def test_anchored_vwap_bar_mode_is_bound_to_one_series() -> None:
    first = candle(0)
    avwap = AnchoredVWAP.from_bar(first)
    avwap.on_candle(first)
    with pytest.raises(ValueError, match="share symbol and timeframe"):
        avwap.on_candle(replace(candle(1), symbol="ETHUSDT"))


def test_confirmed_swing_anchors_carry_lookahead_boundary() -> None:
    lows = ["90", "89", "85", "88", "89", "90", "91"]
    highs = ["105", "106", "107", "108", "125", "109", "108"]
    bars = tuple(candle(i, low=low, high=high) for i, (low, high) in enumerate(zip(lows, highs)))
    anchors = confirmed_swing_anchors(bars, length=2)
    low_anchor = next(anchor for anchor in anchors if anchor.kind == "swing_low")
    high_anchor = next(anchor for anchor in anchors if anchor.kind == "swing_high")
    assert (low_anchor.bar_index, low_anchor.price) == (2, D("85"))
    assert low_anchor.confirmed_at == bars[4].close_time
    assert not low_anchor.is_confirmed(bars[4].close_time - timedelta(microseconds=1))
    assert low_anchor.is_confirmed(bars[4].close_time)
    assert (high_anchor.bar_index, high_anchor.price) == (4, D("125"))
    assert high_anchor.confirmed_at == bars[6].close_time


@pytest.mark.parametrize(
    ("price", "low_avwap", "high_avwap", "expected"),
    [
        ("111", "100", "110", "strong_long"),
        ("99", "100", "110", "strong_short"),
        ("105", "100", "110", "between"),
        ("110", "100", "110", "between"),
        ("105", "110", "100", "between"),
        ("111", "110", "100", "strong_long"),
        ("99", "110", "100", "strong_short"),
        ("0", "100", "110", "unavailable"),
        ("100", None, "110", "unavailable"),
    ],
)
def test_dual_avwap_bias(price, low_avwap, high_avwap, expected) -> None:
    assert dual_avwap_bias(price, low_avwap, high_avwap) == expected
