"""Breakout Continuity: the sequence, and the things that must NOT arm."""

from __future__ import annotations

from vnedge.strategy.arm_sources import BarContext
from vnedge.strategy.breakout_continuity import BreakoutContinuityArmSource

T0 = 1_700_000_000_000


def _bar(i, o, h, l, c, v=100.0):
    return (T0 + i * 300_000, float(o), float(h), float(l), float(c), float(v))


def _ctx(bars, i, atr=1.0, vol_ma=100.0):
    return BarContext(bars=bars, index=i, atr=atr, vol_ma=vol_ma,
                      vwap=bars[i][4], prev_close=bars[i - 1][4])


def _range(n=90, price=100.0):
    """A quiet range: highs at 100.5, lows at 99.5."""
    return [_bar(i, price, price + 0.5, price - 0.5, price) for i in range(n)]


def _drive(source, bars, atr=1.0, vol_ma=100.0, expansion_from=None):
    armed = None
    for i in range(source.warmup_bars, len(bars)):
        a = source.observe(_ctx(bars, i, atr=atr, vol_ma=vol_ma))
        armed = a or armed
    return armed


def _breakout_tape():
    """Range -> impulse break above 100.5 -> shallow retest -> hold -> confirm."""
    bars = _range(90)
    # impulse: big expansion bar on 2x volume closing well above the range
    bars[88] = _bar(88, 100.0, 103.0, 99.9, 102.8, 250.0)
    bars[89] = _bar(89, 102.8, 103.2, 102.5, 103.0, 150.0)
    # retest: dips to just under the broken 100.5 with a long lower wick
    bars.append(_bar(90, 102.0, 102.1, 100.3, 101.8, 140.0))
    # confirmation: green close back above the level
    bars.append(_bar(91, 101.8, 102.6, 101.7, 102.5, 130.0))
    return bars


def test_arms_on_a_clean_break_retest_hold_confirm() -> None:
    source = BreakoutContinuityArmSource(min_confidence=40)
    armed = _drive(source, _breakout_tape())
    assert armed is not None, "a clean breakout-continuity sequence must arm"
    assert armed.side_hint == "long"
    assert "breakout_continuity long" in source.last_reason


def test_never_arms_on_the_impulse_bar_itself() -> None:
    """The break is context; entering on it is chasing the spike."""
    source = BreakoutContinuityArmSource(min_confidence=40)
    bars = _breakout_tape()
    for i in range(source.warmup_bars, 90):  # up to and including the impulse
        assert source.observe(_ctx(bars, i)) is None


def test_a_deep_pullback_is_a_failed_break_not_a_retest() -> None:
    source = BreakoutContinuityArmSource(min_confidence=40)
    bars = _breakout_tape()
    # pull all the way back through the level: depth far beyond max_depth_atr
    bars[90] = _bar(90, 102.0, 102.1, 97.0, 101.8, 140.0)
    assert _drive(source, bars) is None


def test_a_close_back_through_the_level_voids_the_break() -> None:
    source = BreakoutContinuityArmSource(min_confidence=40)
    bars = _breakout_tape()
    bars[90] = _bar(90, 102.0, 102.1, 99.0, 99.2, 140.0)  # closes below level - 0.5 ATR
    assert _drive(source, bars) is None
    assert source._state is None


def test_a_quiet_break_without_expansion_does_not_count() -> None:
    """A break whose bar is no bigger than the preceding tape is not an impulse."""
    source = BreakoutContinuityArmSource(min_confidence=40)
    bars = _breakout_tape()
    # widen the preceding range so the impulse bar is unremarkable against it
    for i in range(60, 88):
        bars[i] = _bar(i, 100.0, 102.0, 98.0, 100.0)
    assert _drive(source, bars) is None


def test_a_break_on_thin_volume_does_not_count() -> None:
    source = BreakoutContinuityArmSource(min_confidence=40)
    bars = _breakout_tape()
    bars[88] = _bar(88, 100.0, 103.0, 99.9, 102.8, 100.0)  # 1x volume
    assert _drive(source, bars) is None


def test_a_stale_break_expires() -> None:
    source = BreakoutContinuityArmSource(min_confidence=40, breakout_expiry_bars=2)
    assert _drive(source, _breakout_tape()) is None


def test_one_continuation_per_break() -> None:
    source = BreakoutContinuityArmSource(min_confidence=40)
    bars = _breakout_tape()
    _drive(source, bars)
    assert source._state is None, "the break must be consumed by its continuation"


def test_the_arm_points_at_the_broken_level() -> None:
    source = BreakoutContinuityArmSource(min_confidence=40)
    armed = _drive(source, _breakout_tape())
    assert armed is not None
    # long: the level is the upper edge, the stop side sits below it
    assert armed.box_high == 100.5
    assert armed.box_low < armed.box_high
