"""Tests for the structure map and the Structure Bounce arm."""

from __future__ import annotations

import numpy as np
import pytest

from vnedge.strategy.arm_sources import BarContext, StructureBounceArmSource
from vnedge.strategy.structure_map import (
    RESISTANCE,
    SUPPORT,
    build_structure_map,
    detect_order_blocks,
    find_horizontal_sr,
    find_liquidity_zones,
    find_swings,
    vwap_bands,
)

T0 = 1_700_000_000_000


def _bar(i, o, h, l, c, v=100.0):
    return (T0 + i * 300_000, float(o), float(h), float(l), float(c), float(v))


def _flat(n, price=100.0, v=100.0):
    return [_bar(i, price, price + 0.1, price - 0.1, price, v) for i in range(n)]


def test_swings_never_read_an_unclosed_bar() -> None:
    """A 3-bar pivot at i is confirmed by i+1; the loop must stop before the end."""
    bars = _flat(30)
    bars[10] = _bar(10, 100, 105, 99.9, 100)   # pivot high
    bars[20] = _bar(20, 100, 100.1, 95, 100)   # pivot low
    highs, lows = find_swings(bars, lookback=30)
    assert 10 in [i for i, _ in highs]
    assert 20 in [i for i, _ in lows]
    assert all(i < len(bars) - 1 for i, _ in highs + lows)


def test_horizontal_sr_needs_repeated_touches() -> None:
    bars = _flat(60)
    # three separate dips to ~99 -> one clustered support zone
    for idx in (12, 28, 44):
        bars[idx] = _bar(idx, 100, 100.1, 99.0, 100)
    levels = find_horizontal_sr(bars, atr=1.0, lookback=60, min_touches=2)
    supports = [x for x in levels if x.side == SUPPORT]
    assert supports, "three equal dips must form a support zone"
    best = max(supports, key=lambda x: x.touch_count)
    assert best.touch_count >= 2
    assert best.zone_low < best.price < best.zone_high
    assert 0 < best.strength <= 100


def test_order_block_is_dropped_once_mitigated() -> None:
    # price must travel AWAY after the impulse, otherwise the block is
    # mitigated by the very next bar and can never be observed unmitigated
    bars = _flat(12)
    bars[10] = _bar(10, 100, 100.2, 99.5, 99.6)          # bearish
    bars[11] = _bar(11, 99.6, 104, 99.5, 103.8)          # bullish impulse
    bars += [_bar(i, 104, 104.2, 103.8, 104) for i in range(12, 30)]
    atrs = [1.0] * len(bars)
    unmitigated = detect_order_blocks(bars, atrs, lookback=30, min_impulse_atr=1.5)
    assert any(x.level_type == "order_block" for x in unmitigated)
    # price later trades back down through the block -> it must disappear
    revisit = list(bars)
    revisit[20] = _bar(20, 104, 104.2, 99.0, 100.0)
    assert not detect_order_blocks(revisit, atrs, lookback=30, min_impulse_atr=1.5)


def test_liquidity_zone_needs_near_equal_pivots() -> None:
    bars = _flat(60, price=100.0)
    bars[15] = _bar(15, 100, 100.1, 98.00, 100)
    bars[35] = _bar(35, 100, 100.1, 98.01, 100)   # equal low within 0.15%
    zones = find_liquidity_zones(bars, lookback=60)
    assert any(z.side == SUPPORT and z.extra.get("liq_type") == "equal_lows" for z in zones)


def test_vwap_bands_are_ordered_and_finite() -> None:
    rng = np.random.default_rng(5)
    closes = 100 + np.cumsum(rng.normal(0, 0.3, 120))
    bars = [_bar(i, c, c + 0.2, c - 0.2, c, 100.0) for i, c in enumerate(closes)]
    vwap, up1, lo1, up2, lo2 = vwap_bands(bars)
    assert lo2 <= lo1 <= vwap <= up1 <= up2
    assert all(np.isfinite(v) for v in (vwap, up1, lo1, up2, lo2))


def test_structure_map_resolves_nearest_levels() -> None:
    bars = _flat(120, price=100.0)
    for idx in (20, 50, 80):
        bars[idx] = _bar(idx, 100, 100.1, 98.5, 100)     # support cluster
    for idx in (30, 60, 90):
        bars[idx] = _bar(idx, 100, 101.5, 99.9, 100)     # resistance cluster
    smap = build_structure_map(bars, [0.5] * len(bars), atr=0.5)
    assert smap.levels
    if smap.nearest_support:
        assert smap.nearest_support.price < bars[-1][4]
    if smap.nearest_resistance:
        assert smap.nearest_resistance.price > bars[-1][4]


def _ctx(bars, i, atr=0.5, vol_ma=100.0):
    return BarContext(bars=bars, index=i, atr=atr, vol_ma=vol_ma,
                      vwap=bars[i - 1][4], prev_close=bars[i - 1][4])


def _bounce_tape():
    """Support cluster at ~98.5, then a rejection wick and a confirming close."""
    bars = _flat(320, price=100.0)
    # inside find_swings' 100-bar scan window: touches outside it are invisible,
    # and the arm then falls through to a VWAP band instead of real structure.
    for idx in (240, 265, 290):
        bars[idx] = _bar(idx, 100, 100.1, 98.5, 100)
    # rejection: long lower wick into the zone, closes back up, heavy volume
    bars[310] = _bar(310, 100.0, 100.1, 98.4, 99.9, 400.0)
    # confirmation: green close
    bars[311] = _bar(311, 99.9, 100.6, 99.85, 100.5, 300.0)
    return bars


def test_bounce_arms_on_the_full_sequence() -> None:
    source = StructureBounceArmSource(map_bars=300, rebuild_every=1, min_confidence=40)
    bars = _bounce_tape()
    armed = None
    for i in range(source.warmup_bars, len(bars)):
        armed = source.observe(_ctx(bars, i)) or armed
    assert armed is not None, "a clean 4-step sequence must arm"
    assert armed.side_hint == "long"
    assert armed.box_low < armed.box_high
    assert source.last_confidence >= 40


def test_bounce_refuses_without_the_confirmation_close() -> None:
    """A red close cannot confirm a LONG bounce.

    It may legitimately arm a SHORT elsewhere on the tape, so the assertion is
    about the long side specifically rather than about silence.
    """
    source = StructureBounceArmSource(map_bars=300, rebuild_every=1, min_confidence=40)
    bars = _bounce_tape()
    bars[311] = _bar(311, 100.5, 100.6, 99.5, 99.6, 300.0)
    longs = []
    for i in range(source.warmup_bars, len(bars)):
        state = source.observe(_ctx(bars, i))
        if state is not None and state.side_hint == "long":
            longs.append(i)
    assert longs == []


def test_bounce_refuses_when_confidence_is_below_threshold() -> None:
    bars = _bounce_tape()
    strict = StructureBounceArmSource(map_bars=300, rebuild_every=1, min_confidence=101)
    armed = None
    for i in range(strict.warmup_bars, len(bars)):
        armed = strict.observe(_ctx(bars, i)) or armed
    assert armed is None


def test_side_hint_makes_the_trigger_anchor_on_the_zone() -> None:
    from vnedge.execution.trigger_engine import ArmState, TriggerEngine

    engine = TriggerEngine()
    arm = ArmState(episode_id=1, box_high=101.0, box_low=99.0, compressed=True,
                   atr=0.5, vol_ma=10.0, prev_close=100.0, side_hint="long")
    fire = engine.try_fire(arm=arm, high=99.2, low=98.9, close=99.05, volume=50.0,
                           vwap=100.0, bar_index=100, bar_ts_ms=T0)
    assert fire is not None
    assert fire.side == "long"
    assert fire.level == pytest.approx(99.0)          # the defended edge
    assert fire.stop == pytest.approx(99.0 - 1.7 * 0.5)  # below the zone


def test_resting_limit_fill_bar_is_managed() -> None:
    """The bar that fills a resting limit must still be checked against the stop.

    It traded THROUGH the limit by construction, so it is the bar most likely
    to carry price on to the stop.  Skipping it hands the position a free bar
    exactly where it is most exposed.
    """
    from vnedge.execution.exit_engine import ExitConfig, ExitEngine
    from vnedge.execution.trigger_engine import ArmState, TriggerConfig, TriggerEngine
    from vnedge.runtime.scanner_session import ScannerSession

    session = ScannerSession(
        symbol="T", arm_source=StructureBounceArmSource(),
        exits=ExitEngine(config=ExitConfig(failed_breakout=False)),
        trigger=TriggerEngine(config=TriggerConfig()),
    )
    # a long resting at 100 with its stop at 99
    session._pending = {
        "side": "long", "entry": 100.0, "stop": 99.0, "risk": 1.0,
        "box_edge": 100.0, "expires": 99, "chase_bps": 0.0,
        "reason": "test", "arm": "structure_bounce",
    }
    # this bar touches the limit AND collapses through the stop
    bars = [_bar(i, 100, 100.5, 99.5, 100) for i in range(5)]
    bars[4] = _bar(4, 100.2, 100.3, 98.0, 98.2)
    session._try_fill(bars, 4, atr=0.5)

    assert session._open is None, "the filling bar must not survive its own stop"
    assert session.trades and session.trades[-1].reason == "stop"
    assert session.trades[-1].exit_price == 99.0
