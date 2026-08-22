"""htf_ma_pullback_4h_v1 — frozen spec. These tests pin the pre-registration."""

from __future__ import annotations

from vnedge.strategy.arm_sources import BarContext
from vnedge.strategy.htf_ma_pullback_4h import HtfMaPullback4hArmSource

T0 = 1_700_000_000_000
STEP = 14_400_000  # 4h


def _bar(i, o, h, l, c, v=100.0):
    return (T0 + i * STEP, float(o), float(h), float(l), float(c), float(v))


def _ctx(bars, i):
    return BarContext(bars=bars, index=i, atr=1.0, vol_ma=100.0,
                      vwap=bars[i][4], prev_close=bars[i - 1][4])


def _drive(src, bars):
    armed = None
    for i in range(len(bars)):
        armed = src.observe(_ctx(bars, i)) or armed
    return armed


def _uptrend(n=120, start=100.0, step=0.6):
    """A steady advance: EMA20 pulls above EMA50."""
    return [_bar(i, start + i * step, start + i * step + 0.5,
                 start + i * step - 0.5, start + i * step) for i in range(n)]


def test_a_pullback_and_reclaim_in_an_uptrend_arms_long() -> None:
    bars = _uptrend()
    top = bars[-1][4]
    # EMA20 lags a 0.6/bar advance by ~5.7, so the dip must reach past that
    bars.append(_bar(120, top, top + 0.2, top - 8.0, top - 7.5))   # touches EMA20
    bars.append(_bar(121, top - 7.5, top + 1.0, top - 7.6, top + 0.8))  # reclaim
    src = HtfMaPullback4hArmSource()
    armed = _drive(src, bars)
    assert armed is not None
    assert armed.side_hint == "long"
    assert src.trend_side == "long"


def test_side_comes_from_the_trend_not_the_entry_bar() -> None:
    """A bullish-looking bar in a downtrend must never arm long.

    This is the property that separates the ID from the two closed families.
    """
    n = 120
    bars = [_bar(i, 200 - i * 0.6, 200 - i * 0.6 + 0.5,
                 200 - i * 0.6 - 0.5, 200 - i * 0.6) for i in range(n)]
    low = bars[-1][4]
    bars.append(_bar(n, low, low + 4.0, low - 0.2, low + 3.5))      # strong green
    bars.append(_bar(n + 1, low + 3.5, low + 3.6, low - 1.0, low - 0.8))
    src = HtfMaPullback4hArmSource()
    armed = _drive(src, bars)
    assert src.trend_side == "short"
    if armed is not None:
        assert armed.side_hint == "short", "must never arm against the 4h trend"


def test_no_arm_without_a_pullback() -> None:
    """A trend that never returns to the EMA gives no entry."""
    src = HtfMaPullback4hArmSource()
    assert _drive(src, _uptrend()) is None


def test_the_touching_bar_itself_does_not_trigger() -> None:
    """Touch and reclaim must be separate bars — same discipline as the break arm."""
    bars = _uptrend()
    top = bars[-1][4]
    # one bar that both dips to the EMA and closes back above it
    bars.append(_bar(120, top, top + 1.0, top - 8.0, top + 0.8))
    src = HtfMaPullback4hArmSource()
    assert _drive(src, bars) is None


def test_the_stop_sits_two_atr_beyond_the_pullback_extreme() -> None:
    bars = _uptrend()
    top = bars[-1][4]
    bars.append(_bar(120, top, top + 0.2, top - 8.0, top - 7.5))
    bars.append(_bar(121, top - 7.5, top + 1.0, top - 7.6, top + 0.8))
    src = HtfMaPullback4hArmSource()
    armed = _drive(src, bars)
    assert armed is not None
    level = armed.box_low                       # long: trigger reads level here
    implied_stop = level - (armed.box_high - armed.box_low)
    assert abs(implied_stop - (top - 8.0 - 2.0 * armed.atr)) < 1e-6


def test_a_trend_flip_voids_a_half_formed_setup() -> None:
    src = HtfMaPullback4hArmSource()
    src.trend_side = "long"
    src._pulled_back = True
    src._pullback_extreme = 100.0
    bars = [_bar(i, 200 - i, 200 - i + 0.5, 200 - i - 0.5, 200 - i)
            for i in range(120)]
    _drive(src, bars)
    assert src.trend_side == "short"
    assert src._pullback_extreme is None


def test_frozen_parameters_match_the_preregistration() -> None:
    p = HtfMaPullback4hArmSource()
    assert (p.fast_ema, p.slow_ema, p.atr_bars) == (20, 50, 14)
    assert p.stop_atr_mult == 2.0
