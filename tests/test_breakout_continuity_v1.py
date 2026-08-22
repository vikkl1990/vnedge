"""breakout_continuity_v1 — frozen spec. These tests pin the specification.

A failure here means the implementation drifted from
docs/prereg/breakout_continuity_v1_20260821.md, which is a pre-registration
violation, not a bug to paper over.
"""

from __future__ import annotations

from vnedge.strategy.arm_sources import BarContext
from vnedge.strategy.breakout_continuity_v1 import BreakoutContinuityV1ArmSource

T0 = 1_700_000_000_000
STEP = 900_000  # 15m


def _bar(i, o, h, l, c, v=100.0):
    return (T0 + i * STEP, float(o), float(h), float(l), float(c), float(v))


def _ctx(bars, i):
    return BarContext(bars=bars, index=i, atr=1.0, vol_ma=100.0,
                      vwap=bars[i][4], prev_close=bars[i - 1][4])


def _base(n=40):
    """A tight Donchian box: 99.8-100.2, so ATR is ~0.4 and depths are legible.

    A loose base makes ATR large relative to the box, and every distance
    expressed in ATR then swamps the geometry the test is trying to pin.
    """
    return [_bar(i, 100, 100.2, 99.8, 100) for i in range(n)]


def _drive(src, bars, upto=None):
    armed = None
    for i in range(src.warmup_bars, upto or len(bars)):
        armed = src.observe(_ctx(bars, i)) or armed
    return armed


def _sequence():
    """break above 101 -> pullback to the level -> reclaim."""
    bars = _base(40)
    bars.append(_bar(40, 100.1, 101.2, 100.05, 101.0))  # close > 100.2 + 0.05*ATR
    bars.append(_bar(41, 101.0, 101.1, 100.15, 100.5))  # pullback tags the level
    bars.append(_bar(42, 100.5, 100.9, 100.40, 100.8))  # reclaim above 100.2
    return bars


def test_the_frozen_sequence_arms_long() -> None:
    src = BreakoutContinuityV1ArmSource()
    armed = _drive(src, _sequence())
    assert armed is not None
    assert armed.side_hint == "long"
    assert "breakout_continuity_v1 long" in src.last_reason


def test_never_arms_on_the_break_bar() -> None:
    """Entering on the break is chasing the spike; the spec enters on reclaim."""
    src = BreakoutContinuityV1ArmSource()
    assert _drive(src, _sequence(), upto=41) is None


def test_a_wick_only_break_does_not_count() -> None:
    """The spec requires a CLOSE beyond the level plus an ATR margin."""
    bars = _base(40)
    bars.append(_bar(40, 100.1, 101.2, 100.05, 100.15))  # high pierces, close does not
    bars.append(_bar(41, 100.15, 100.2, 100.0, 100.1))
    bars.append(_bar(42, 100.1, 100.9, 100.05, 100.8))
    assert _drive(BreakoutContinuityV1ArmSource(), bars) is None


def test_a_deep_pullback_voids_the_break() -> None:
    bars = _sequence()
    # invalidation is 1.0 ATR (~0.4) below the 100.2 level, i.e. below ~99.8
    bars[41] = _bar(41, 101.0, 101.1, 99.3, 99.4)
    assert _drive(BreakoutContinuityV1ArmSource(), bars) is None


def test_a_late_reclaim_expires() -> None:
    """Reclaim must come within pullback_window bars of the pullback."""
    bars = _sequence()[:42]
    for k in range(42, 60):
        bars.append(_bar(k, 100.5, 100.6, 100.0, 100.1))  # loiters below, no reclaim
    bars.append(_bar(60, 100.1, 100.9, 100.05, 100.8))    # far too late
    assert _drive(BreakoutContinuityV1ArmSource(), bars) is None


def test_the_stop_sits_beyond_the_pullback_extreme() -> None:
    """box span encodes the stop: level - span = pullback low - 0.2 ATR."""
    src = BreakoutContinuityV1ArmSource()
    armed = _drive(src, _sequence())
    assert armed is not None
    level = armed.box_low                      # long: trigger reads level from box_low
    span = armed.box_high - armed.box_low
    implied_stop = level - span
    assert implied_stop < 100.15               # below the pullback low
    assert abs(implied_stop - (100.15 - 0.2 * armed.atr)) < 1e-6


def test_one_continuation_per_break() -> None:
    src = BreakoutContinuityV1ArmSource()
    _drive(src, _sequence())
    assert src._state is None


def test_frozen_parameters_match_the_preregistration() -> None:
    """Changing any of these requires a NEW pre-registration ID, not an edit."""
    p = BreakoutContinuityV1ArmSource()
    assert (p.donchian_bars, p.atr_bars) == (20, 14)
    assert p.break_atr_margin == 0.05
    assert p.pullback_window == 12
    assert p.stop_atr_beyond == 0.2
