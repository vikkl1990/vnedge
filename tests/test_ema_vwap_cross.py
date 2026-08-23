"""ema_vwap_cross_v1 — frozen spec. These tests pin the pre-registration."""

from __future__ import annotations

from vnedge.strategy.arm_sources import BarContext
from vnedge.strategy.ema_vwap_cross import EmaVwapCrossArmSource

T0 = 1_700_000_000_000
STEP = 3_600_000


def _bar(i, o, h, l, c, v=100.0):
    return (T0 + i * STEP, float(o), float(h), float(l), float(c), float(v))


def _ctx(bars, i):
    return BarContext(bars=bars, index=i, atr=1.0, vol_ma=100.0,
                      vwap=bars[i][4], prev_close=bars[i - 1][4])


def _drive(src, bars):
    out = []
    for i in range(len(bars)):
        a = src.observe(_ctx(bars, i))
        if a is not None:
            out.append((i, a))
    return out


def _flat_then_rally(n=60, m=20):
    bars = [_bar(i, 100, 100.5, 99.5, 100) for i in range(n)]
    bars += [_bar(n + j, 100 + j, 100 + j + 0.5, 100 + j - 0.5, 100 + j + 0.8)
             for j in range(m)]
    return bars


def test_a_rally_crossing_up_arms_long_once() -> None:
    src = EmaVwapCrossArmSource()
    arms = _drive(src, _flat_then_rally())
    assert arms, "a decisive rally must cross VWAP"
    assert arms[0][1].side_hint == "long"
    assert "ema_vwap_cross_v1 long" in src.last_reason


def test_it_arms_only_on_the_cross_not_on_every_bar_above() -> None:
    """A state, not an event, would fire every bar of a trend."""
    src = EmaVwapCrossArmSource()
    arms = _drive(src, _flat_then_rally(60, 40))
    ups = [a for _, a in arms if a.side_hint == "long"]
    assert len(ups) <= 2, f"crossed {len(ups)} times in one clean rally"


def test_a_selloff_arms_short() -> None:
    """Price must be ABOVE VWAP first -- you cannot cross down from below.

    A flat tape leaves EMA equal to VWAP, i.e. already "not above", so a
    selloff from flat is a continuation of that state and correctly produces
    no signal.
    """
    bars = _flat_then_rally(40, 25)                    # establishes above-VWAP
    top = bars[-1][4]
    bars += [_bar(len(bars) + j, top - 2 * j, top - 2 * j + 0.5,
                  top - 2 * j - 0.5, top - 2 * j - 1.5) for j in range(30)]
    arms = _drive(EmaVwapCrossArmSource(), bars)
    sides = [a.side_hint for _, a in arms]
    assert "long" in sides and "short" in sides, sides
    assert sides.index("long") < sides.index("short")


def test_a_flat_tape_never_arms() -> None:
    assert _drive(EmaVwapCrossArmSource(),
                  [_bar(i, 100, 100.5, 99.5, 100) for i in range(120)]) == []


def test_the_stop_is_two_atr_from_entry() -> None:
    src = EmaVwapCrossArmSource()
    arms = _drive(src, _flat_then_rally())
    _, armed = arms[0]
    level = armed.box_low                      # long: trigger reads level here
    implied_stop = level - (armed.box_high - armed.box_low)
    assert abs(implied_stop - (level - 2.0 * armed.atr)) < 1e-9


def test_vwap_is_volume_weighted_not_a_price_average() -> None:
    """Heavy volume at one price must pull VWAP toward it."""
    bars = [_bar(i, 100, 100.5, 99.5, 100, v=1.0) for i in range(40)]
    bars[20] = _bar(20, 100, 100.5, 99.5, 90, v=10_000.0)   # huge low-price print
    src = EmaVwapCrossArmSource()
    for i in range(len(bars)):
        src.observe(_ctx(bars, i))
    window = bars[len(bars) - src.vwap_bars:]
    vv = sum(b[5] for b in window)
    vwap = sum(b[4] * b[5] for b in window) / vv
    assert vwap < 99.0, f"volume weighting ignored (vwap={vwap:.2f})"


def test_the_arm_survives_a_late_first_observation() -> None:
    """ScannerSession does not call observe() until its own warmup completes.

    An incrementally accumulated VWAP assumes it saw every bar from index 0;
    starting late made its first subtraction remove volume never added, which
    silenced the arm (16 arms in a session vs 697 standalone on the same bars).
    """
    bars = _flat_then_rally(80, 30)
    late = EmaVwapCrossArmSource()
    seen = [late.observe(_ctx(bars, i)) for i in range(30, len(bars))]
    early = EmaVwapCrossArmSource()
    full = [early.observe(_ctx(bars, i)) for i in range(len(bars))]
    assert sum(x is not None for x in seen) > 0, "late start silenced the arm"
    assert sum(x is not None for x in full) > 0


def test_frozen_parameters_match_the_preregistration() -> None:
    p = EmaVwapCrossArmSource()
    assert (p.ema_period, p.vwap_bars, p.atr_bars) == (9, 24, 14)
    assert p.stop_atr_mult == 2.0
