"""Tests for the pluggable arm sources and the shared scanner session."""

from __future__ import annotations

import numpy as np
import pytest

from vnedge.runtime.scanner_session import (
    ScannerSession,
    SessionCosts,
    summarize,
)
from vnedge.strategy.arm_sources import (
    BarContext,
    CoilArmSource,
    CompositeArmSource,
    IgnitionArmSource,
)

T0 = 1_700_000_000_000


def _bars(closes, volumes=None, spread_frac=0.0006):
    closes = np.asarray(closes, dtype=float)
    volumes = np.asarray(volumes if volumes is not None else np.full(len(closes), 100.0), dtype=float)
    spread = closes * spread_frac
    # open = prior close so bars have real bodies (a zero-body bar can never
    # register as a thrust, which would make ignition tests vacuous)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return [
        (
            T0 + i * 300_000,
            float(opens[i]),
            float(max(opens[i], closes[i]) + spread[i]),
            float(min(opens[i], closes[i]) - spread[i]),
            float(closes[i]),
            float(volumes[i]),
        )
        for i in range(len(closes))
    ]


def _ctx(bars, i, *, atr=10.0, vol_ma=100.0, vwap=None):
    return BarContext(
        bars=bars, index=i, atr=atr, vol_ma=vol_ma,
        vwap=vwap if vwap is not None else bars[i - 1][4] * 0.99,
        prev_close=bars[i - 1][4],
    )


def test_coil_source_needs_full_rank_history() -> None:
    source = CoilArmSource(compression_bars=4, rank_lookback=20)
    bars = _bars(np.full(30, 100.0))
    # before the rank window fills, nothing may arm
    assert source.observe(_ctx(bars, 6)) is None


def test_coil_source_arms_on_a_narrow_box() -> None:
    rng = np.random.default_rng(2)
    wide = 100 + np.cumsum(rng.normal(0, 1.2, 260))
    tight = np.full(30, float(wide[-1]))
    bars = _bars(np.concatenate([wide, tight]))
    source = CoilArmSource(compression_bars=8, rank_lookback=200, threshold=0.20)
    armed = None
    for i in range(9, len(bars)):
        armed = source.observe(_ctx(bars, i))
    assert armed is not None
    assert armed.compressed
    assert armed.box_high >= armed.box_low


def test_coil_absolute_floor_can_arm_when_rank_does_not() -> None:
    rng = np.random.default_rng(3)
    # a persistently calm tape: nothing ranks in the bottom quintile
    closes = 100 + np.cumsum(rng.normal(0, 0.01, 300))
    bars = _bars(closes, spread_frac=0.00002)
    strict = CoilArmSource(compression_bars=8, rank_lookback=200, threshold=0.05)
    loose = CoilArmSource(
        compression_bars=8, rank_lookback=200, threshold=0.05, absolute_floor_bps=500.0
    )
    strict_arms = sum(strict.observe(_ctx(bars, i)) is not None for i in range(9, len(bars)))
    loose_arms = sum(loose.observe(_ctx(bars, i)) is not None for i in range(9, len(bars)))
    assert loose_arms > strict_arms


def test_ignition_source_requires_body_and_volume() -> None:
    source = IgnitionArmSource(box_bars=4, body_fraction=0.6, volume_mult=2.5)
    flat = _bars(np.full(20, 100.0))
    assert source.observe(_ctx(flat, 10, vol_ma=100.0)) is None
    # a wide-bodied bar on heavy volume
    bars = list(flat)
    bars[10] = (bars[10][0], 100.0, 103.0, 99.9, 102.8, 400.0)
    assert source.observe(_ctx(bars, 10, vol_ma=100.0)) is not None
    # same body, quiet volume -> no arm
    bars[10] = (bars[10][0], 100.0, 103.0, 99.9, 102.8, 100.0)
    assert source.observe(_ctx(bars, 10, vol_ma=100.0)) is None


def test_ignition_episodes_never_collide_with_coil_episodes() -> None:
    flat = _bars(np.full(20, 100.0))
    bars = list(flat)
    bars[10] = (bars[10][0], 100.0, 103.0, 99.9, 102.8, 400.0)
    armed = IgnitionArmSource(box_bars=4).observe(_ctx(bars, 10, vol_ma=100.0))
    assert armed is not None
    assert armed.episode_id < 0  # coil episodes count up from 1


def test_composite_prefers_the_first_source_and_records_the_winner() -> None:
    flat = _bars(np.full(20, 100.0))
    bars = list(flat)
    bars[10] = (bars[10][0], 100.0, 103.0, 99.9, 102.8, 400.0)
    composite = CompositeArmSource(
        sources=[CoilArmSource(compression_bars=4, rank_lookback=500), IgnitionArmSource(box_bars=4)]
    )
    armed = composite.observe(_ctx(bars, 10, vol_ma=100.0))
    assert armed is not None
    # coil cannot arm (rank history unfilled), so ignition wins and is named
    assert composite.last_armed == "ignition"


def test_scalper_offer_waives_the_closing_leg() -> None:
    costs = SessionCosts(taker_bps=5.9, free_close_within_bars=6)
    assert costs.round_trip_bps(3) == pytest.approx(5.9)
    assert costs.round_trip_bps(9) == pytest.approx(11.8)


def test_session_runs_a_full_trade_and_reports() -> None:
    rng = np.random.default_rng(11)
    base = 100 + np.cumsum(rng.normal(0, 0.4, 200))
    flat = np.full(60, float(base[-1]))
    # gentle enough that the first break stays inside the 20 bps chase cap;
    # a steeper leg is correctly refused as "move already gone"
    leg = [float(flat[-1]) * (1 + k * 0.002) for k in range(1, 14)]
    # a pullback after the leg so the trailing stop resolves the position:
    # an open position at the end of the tape is deliberately NOT a trade
    fade = [leg[-1] * (1 - k * 0.003) for k in range(1, 12)]
    closes = np.concatenate([base, flat, leg, fade])
    volumes = np.full(len(closes), 100.0)
    volumes[len(base) + len(flat):] = 500.0
    # tight synthetic spread so a real body dominates the bar's span
    bars = _bars(closes, volumes, spread_frac=0.0002)
    session = ScannerSession(symbol="TEST", arm_source=IgnitionArmSource(box_bars=12))
    trades = session.run(bars)
    assert trades, "an ignition leg should produce at least one trade"
    for trade in trades:
        assert trade.arm == "ignition"
        assert trade.exit_index > trade.entry_index
        assert trade.net_bps == pytest.approx(trade.gross_bps - trade.fee_bps)
    report = summarize(trades)
    assert report["n"] == len(trades)


def test_arm_source_observes_every_bar_even_while_positioned() -> None:
    """Rolling arm state must not develop gaps during an open position."""

    class _Counter:
        name = "counter"

        def __init__(self) -> None:
            self.seen = 0

        def observe(self, ctx):
            self.seen += 1
            return None

    bars = _bars(100 + np.cumsum(np.random.default_rng(1).normal(0, 0.5, 300)))
    counter = _Counter()
    session = ScannerSession(symbol="TEST", arm_source=counter)
    session.run(bars)
    # every bar past the feature warmup reaches the source
    assert counter.seen == len(bars) - (session.config.atr_period + 1)
