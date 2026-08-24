"""Tests for the trigger/exit plane engines (reviewed spec 2026-08-18)."""

from __future__ import annotations

import pytest

from vnedge.execution.exit_engine import ExitConfig, ExitEngine
from vnedge.execution.trigger_engine import ArmState, TriggerConfig, TriggerEngine

TS0 = 1_787_000_000_000  # fixed UTC ms anchor


def _arm(episode: int = 1, compressed: bool = True) -> ArmState:
    return ArmState(
        episode_id=episode,
        box_high=60_100.0,
        box_low=59_900.0,
        compressed=compressed,
        atr=60.0,
        vol_ma=100.0,
        prev_close=60_050.0,
    )


def _fire(
    engine: TriggerEngine,
    *,
    close: float,
    bar_index: int = 100,
    volume: float = 200.0,
    vwap: float = 59_000.0,
    episode: int = 1,
    ts: int = TS0,
):
    return engine.try_fire(
        arm=_arm(episode=episode),
        high=close + 5,
        low=close - 5,
        close=close,
        volume=volume,
        vwap=vwap,
        bar_index=bar_index,
        bar_ts_ms=ts,
    )


def test_fires_long_above_level_with_level_anchored_stop() -> None:
    eng = TriggerEngine()
    fire = _fire(eng, close=60_130.0)
    assert fire is not None and fire.side == "long"
    level = 60_100.0 + 60_050.0 * 2.0 / 10_000
    assert fire.level == pytest.approx(level)
    assert fire.stop == pytest.approx(level - 1.7 * 60.0)  # anchored to level, not fill
    assert fire.chase_bps <= TriggerConfig().max_chase_bps


def test_chase_cap_burns_the_episode() -> None:
    eng = TriggerEngine()
    late_close = 60_100.0 * (1 + 30 / 10_000)  # ~30bps past the level
    assert _fire(eng, close=late_close) is None
    assert eng.fired_episode == 1
    # even a perfect later break of the same episode stays burned
    assert _fire(eng, close=60_130.0, bar_index=200) is None


def test_one_net_position() -> None:
    eng = TriggerEngine()
    assert _fire(eng, close=60_130.0) is not None
    assert _fire(eng, close=60_130.0, bar_index=150, episode=2) is None  # open blocks


def test_cooldown_after_loss_blocks_refire() -> None:
    eng = TriggerEngine()
    assert _fire(eng, close=60_130.0) is not None
    eng.notify_flat(110, won=False)
    assert _fire(eng, close=60_130.0, bar_index=115, episode=2) is None  # inside 9-bar cd
    assert _fire(eng, close=60_130.0, bar_index=125, episode=2) is not None


def test_day_budget_resets_on_utc_roll() -> None:
    eng = TriggerEngine(
        config=TriggerConfig(
            max_fires_per_day=1, min_bars_between_fires=1, cooldown_win_bars=0, cooldown_loss_bars=0
        )
    )
    assert _fire(eng, close=60_130.0, bar_index=10) is not None
    eng.notify_flat(11, won=True)
    assert _fire(eng, close=60_130.0, bar_index=20, episode=2) is None  # budget spent
    next_day = TS0 + 86_400_000
    assert _fire(eng, close=60_130.0, bar_index=30, episode=3, ts=next_day) is not None


def test_vwap_side_veto() -> None:
    eng = TriggerEngine()
    # prev_close (60_050) below vwap -> long fires are vetoed
    assert _fire(eng, close=60_130.0, vwap=61_000.0) is None


def _open_long(
    eng: ExitEngine,
    entry: float = 60_112.0,
    stop: float = 60_000.0,
    risk: float = 102.0,
    box_edge: float = 60_100.0,
) -> None:
    eng.open_from_fire(
        side="long", entry=entry, stop=stop, risk=risk, box_edge=box_edge, entry_bar=100
    )


def test_stop_first_within_bar() -> None:
    eng = ExitEngine()
    _open_long(eng)
    decision = eng.on_bar(high=60_500.0, low=59_990.0, close=60_400.0, atr=60.0, bar_index=101)
    assert decision is not None and decision.reason == "stop" and not decision.won


def test_failed_breakout_close_back_inside_box() -> None:
    eng = ExitEngine()
    _open_long(eng)
    decision = eng.on_bar(high=60_150.0, low=60_050.0, close=60_060.0, atr=60.0, bar_index=101)
    assert decision is not None and decision.reason == "failed_breakout"


def test_no_progress_time_stop() -> None:
    eng = ExitEngine(config=ExitConfig(no_progress_bars=4))
    _open_long(eng)
    decision = None
    for k in range(1, 6):
        decision = eng.on_bar(
            high=60_120.0, low=60_105.0, close=60_115.0, atr=60.0, bar_index=100 + k
        )
        if decision:
            break
    assert decision is not None and decision.reason == "no_progress"


def test_breakeven_ratchet_after_one_r() -> None:
    eng = ExitEngine()
    _open_long(eng)
    # +1R excursion, close stays above the box -> no exit, stop ratchets to BE+fees
    decision = eng.on_bar(
        high=60_112.0 + 102.0, low=60_110.0, close=60_200.0, atr=60.0, bar_index=101
    )
    assert decision is None
    assert eng.pos is not None
    assert eng.pos.stop >= 60_112.0 * (1 + 5.9 / 10_000)


def test_trail_after_two_r_and_winning_stop_exit() -> None:
    eng = ExitEngine()
    _open_long(eng)
    assert (
        eng.on_bar(high=60_112.0 + 250.0, low=60_110.0, close=60_350.0, atr=60.0, bar_index=101)
        is None
    )
    assert eng.pos is not None and eng.pos.stop >= 60_362.0 - 60.0  # extreme - 1*ATR
    trailed_stop = eng.pos.stop
    decision = eng.on_bar(
        high=60_360.0, low=trailed_stop - 1, close=trailed_stop - 1, atr=60.0, bar_index=102
    )
    assert decision is not None and decision.won
    # the ratchet moved this stop, so it reports as a breakeven stop -- the same
    # name runtime.active_exit uses, so exit histograms merge across surfaces
    assert decision.reason == "breakeven_stop"


def test_tick_protective_stop() -> None:
    eng = ExitEngine()
    _open_long(eng)
    assert eng.on_tick(price=60_050.0) is None
    decision = eng.on_tick(price=59_999.0)
    assert decision is not None and decision.reason == "stop_tick"
    assert eng.pos is None


def test_strategy_deterioration_closes_through_canonical_exit_engine() -> None:
    eng = ExitEngine()
    _open_long(eng)

    decision = eng.close_now(
        price=60_175.0,
        reason="htf_structure_deterioration",
    )

    assert decision is not None
    assert decision.reason == "htf_structure_deterioration"
    assert decision.won is True
    assert eng.pos is None


def test_double_open_refused() -> None:
    eng = ExitEngine()
    _open_long(eng)
    with pytest.raises(ValueError):
        _open_long(eng)
