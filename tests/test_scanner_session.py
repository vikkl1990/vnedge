"""Tests for the pluggable arm sources and the shared scanner session."""

from __future__ import annotations

import datetime as dt
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


def test_reject_codes_are_recorded_for_every_refusal() -> None:
    from vnedge.execution.trigger_engine import ArmState, RejectCode, TriggerEngine

    engine = TriggerEngine()
    arm = ArmState(episode_id=1, box_high=100.0, box_low=99.0, compressed=True,
                   atr=0.5, vol_ma=10.0, prev_close=99.5)
    common = dict(high=101.0, low=98.9, volume=50.0, vwap=99.0,
                  bar_index=100, bar_ts_ms=1_787_000_000_000)

    # no break: close inside the box
    assert engine.try_fire(arm=arm, close=99.5, **common) is None
    assert engine.last_reject is RejectCode.NO_BREAK

    # thin volume ON A REAL BREAK (structure is asked first, so this is
    # attributable: the box broke and the volume was not there)
    assert engine.try_fire(arm=arm, close=100.5,
                           **{**common, "volume": 1.0}) is None
    assert engine.last_reject is RejectCode.VOLUME

    # a quiet bar with thin volume counts as no_break, not volume
    assert engine.try_fire(arm=arm, close=99.5,
                           **{**common, "volume": 1.0}) is None
    assert engine.last_reject is RejectCode.NO_BREAK

    # wrong side of VWAP for a long
    assert engine.try_fire(arm=arm, close=100.5,
                           **{**common, "vwap": 200.0}) is None
    assert engine.last_reject is RejectCode.VWAP_SIDE

    # not compressed
    loose = ArmState(episode_id=2, box_high=100.0, box_low=99.0, compressed=False,
                     atr=0.5, vol_ma=10.0, prev_close=99.5)
    assert engine.try_fire(arm=loose, close=100.5, **common) is None
    assert engine.last_reject is RejectCode.NOT_COMPRESSED

    # every refusal was counted exactly once
    assert sum(engine.reject_counts.values()) == 5
    assert engine.reject_counts["no_break"] == 2


def test_chase_burn_is_its_own_reject_code() -> None:
    from vnedge.execution.trigger_engine import ArmState, RejectCode, TriggerEngine

    engine = TriggerEngine()
    arm = ArmState(episode_id=7, box_high=100.0, box_low=99.0, compressed=True,
                   atr=0.5, vol_ma=10.0, prev_close=99.5)
    assert engine.try_fire(arm=arm, high=104.0, low=99.9, close=103.0, volume=50.0,
                           vwap=99.0, bar_index=100,
                           bar_ts_ms=1_787_000_000_000) is None
    assert engine.last_reject is RejectCode.CHASE_BURN
    assert engine.fired_episode == 7


def test_a_successful_fire_clears_the_reject_state() -> None:
    from vnedge.execution.trigger_engine import ArmState, TriggerEngine

    engine = TriggerEngine()
    arm = ArmState(episode_id=1, box_high=100.0, box_low=99.0, compressed=True,
                   atr=0.5, vol_ma=10.0, prev_close=99.5)
    fire = engine.try_fire(arm=arm, high=100.6, low=99.9, close=100.1, volume=50.0,
                           vwap=99.0, bar_index=100, bar_ts_ms=1_787_000_000_000)
    assert fire is not None
    assert engine.last_reject is None


def test_daily_returns_zero_fill_idle_days() -> None:
    from vnedge.runtime.scanner_session import ScannerTrade, daily_returns_bps

    def _trade(day: int, net: float) -> ScannerTrade:
        ts = int(dt.datetime(2026, 8, day, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
        return ScannerTrade(symbol="T", arm="coil", side="long", entry_index=0,
                            exit_index=1, entry_ts_ms=ts, exit_ts_ms=ts + 300_000,
                            entry_price=1.0, exit_price=1.0, reason="stop",
                            held_bars=1, net_bps=net, gross_bps=net, fee_bps=0.0,
                            chase_bps=0.0)

    series = daily_returns_bps([_trade(1, 10.0), _trade(4, -4.0)])
    # 1 Aug and 4 Aug traded; 2 and 3 must appear as zeros, not be dropped
    assert series == [10.0, 0.0, 0.0, -4.0]


def test_summarize_reports_drawdown_and_never_claims_a_single_config_dsr() -> None:
    from vnedge.runtime.scanner_session import ScannerTrade, summarize

    def _trade(day: int, net: float) -> ScannerTrade:
        ts = int(dt.datetime(2026, 8, day, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
        return ScannerTrade(symbol="T", arm="coil", side="long", entry_index=0,
                            exit_index=1, entry_ts_ms=ts, exit_ts_ms=ts + 300_000,
                            entry_price=1.0, exit_price=1.0, reason="stop",
                            held_bars=1, net_bps=net, gross_bps=net, fee_bps=0.0,
                            chase_bps=0.0)

    trades = [_trade(d, v) for d, v in enumerate([20.0, -30.0, 15.0, -5.0, 25.0], start=1)]
    report = summarize(trades, 3000.0)
    assert report["max_dd_usd"] < 0  # a real peak-to-trough happened
    # DSR is deliberately absent: it cannot be computed from one config
    assert "dsr" not in report
    assert report["psr"] == report["psr"] or len(trades) < 8
