"""Tests for the production gating plane and the scale-out exit ladder."""

from __future__ import annotations

import pytest

from vnedge.execution.exit_engine import ExitConfig, ExitEngine
from vnedge.execution.trigger_engine import TriggerConfig
from vnedge.strategy.production_filters import (
    ExpectancyEngine,
    ProductionGate,
    StreamingRegime,
    session_of,
)

T0 = 1_700_000_000_000


def _bar(i, o, h, l, c, v=100.0):
    return (T0 + i * 300_000, float(o), float(h), float(l), float(c), float(v))


# --- exit ladder ---------------------------------------------------------


def _ladder_engine(**kw):
    cfg = ExitConfig(
        failed_breakout=False, no_progress_bars=999, absolute_max_bars=288,
        tp_ladder=((1.0, 0.5), (2.0, 0.5)), **kw,
    )
    engine = ExitEngine(config=cfg)
    engine.open_from_fire(
        side="long", entry=100.0, stop=99.0, risk=1.0, box_edge=100.0, entry_bar=0
    )
    return engine


def test_ladder_banks_each_rung_and_blends_the_exit_price() -> None:
    engine = _ladder_engine(breakeven_after_tp1=False)
    assert engine.on_bar(high=101.0, low=100.0, close=101.0, atr=1.0, bar_index=1) is None
    assert engine.pos is not None and engine.pos.rungs_filled == 1
    decision = engine.on_bar(high=102.0, low=101.0, close=102.0, atr=1.0, bar_index=2)
    assert decision is not None and decision.reason == "tp_ladder"
    # 0.5 banked at +1.0 and 0.5 at +2.0 -> blended +1.5 over a 100 entry
    assert decision.price == pytest.approx(101.5)
    assert decision.won


def test_stop_after_a_partial_blends_the_banked_rung() -> None:
    # ratchets pushed out of the way so the blend is the only thing measured
    engine = _ladder_engine(
        breakeven_after_tp1=False, breakeven_arm_r=5.0, trail_arm_r=6.0
    )
    engine.on_bar(high=101.0, low=100.0, close=101.0, atr=1.0, bar_index=1)
    decision = engine.on_bar(high=100.5, low=98.0, close=98.5, atr=1.0, bar_index=2)
    assert decision is not None and decision.reason == "stop"
    # +1.0 on half, -1.0 on the other half -> flat, not a full loss
    assert decision.price == pytest.approx(100.0)
    assert not decision.won


def test_ratchet_and_ladder_compose_so_a_partial_cannot_end_negative() -> None:
    """After one rung the +1R breakeven ratchet is already armed.

    The two mechanisms are independent, and a reversal that stops out a
    half-position must still book a profit rather than giving the rung back.
    """
    engine = _ladder_engine(breakeven_after_tp1=False)
    engine.on_bar(high=101.0, low=100.0, close=101.0, atr=1.0, bar_index=1)
    assert engine.pos is not None and engine.pos.stop > 100.0
    decision = engine.on_bar(high=100.5, low=98.0, close=98.5, atr=1.0, bar_index=2)
    # breakeven_after_tp1 is off, so the +1R ratchet armed this stop; either
    # route now reports under runtime.active_exit's "breakeven_stop" name
    assert decision is not None and decision.reason == "breakeven_stop"
    assert decision.price > 100.0 and decision.won


def test_stop_wins_ties_against_a_rung_in_the_same_bar() -> None:
    engine = _ladder_engine(breakeven_after_tp1=False)
    decision = engine.on_bar(high=101.5, low=99.0, close=100.0, atr=1.0, bar_index=1)
    assert decision is not None and decision.reason == "stop"
    assert decision.price == pytest.approx(99.0)


def test_breakeven_after_tp1_moves_the_stop_above_entry() -> None:
    engine = _ladder_engine(breakeven_after_tp1=True)
    engine.on_bar(high=101.0, low=100.0, close=101.0, atr=1.0, bar_index=1)
    assert engine.pos is not None and engine.pos.stop > 100.0


def test_max_age_closes_at_the_cap() -> None:
    cfg = ExitConfig(failed_breakout=False, no_progress_bars=999, max_age_bars=3)
    engine = ExitEngine(config=cfg)
    engine.open_from_fire(
        side="long", entry=100.0, stop=99.0, risk=1.0, box_edge=100.0, entry_bar=0
    )
    for i in (1, 2):
        assert engine.on_bar(high=100.2, low=99.9, close=100.1, atr=1.0, bar_index=i) is None
    decision = engine.on_bar(high=100.3, low=99.9, close=100.2, atr=1.0, bar_index=3)
    assert decision is not None and decision.reason == "max_age"


def test_ladder_rejects_descending_or_oversized_rungs() -> None:
    with pytest.raises(ValueError):
        ExitConfig(tp_ladder=((2.0, 0.5), (1.0, 0.5)))
    with pytest.raises(ValueError):
        ExitConfig(tp_ladder=((1.0, 0.7), (2.0, 0.7)))


# --- percentage-band stops -----------------------------------------------


def test_stop_distance_clamps_into_the_percentage_band() -> None:
    cfg = TriggerConfig(atr_stop_mult=2.5, stop_pct_floor=0.0055, stop_pct_cap=0.0095)
    # a 5m ATR far too tight for a multi-hour thesis is lifted to the floor
    assert cfg.stop_distance(atr=1.0, level=10_000.0) == pytest.approx(55.0)
    # and a wild ATR is capped rather than risking the whole account
    assert cfg.stop_distance(atr=1_000.0, level=10_000.0) == pytest.approx(95.0)
    # inside the band the ATR governs
    assert cfg.stop_distance(atr=30.0, level=10_000.0) == pytest.approx(75.0)


# --- regime detector ------------------------------------------------------


def test_adx_rises_on_a_trend_and_stays_low_in_chop() -> None:
    trend = StreamingRegime()
    snap = None
    for i in range(120):
        price = 100.0 + i * 0.5
        snap = trend.update(_bar(i, price, price + 0.3, price - 0.1, price + 0.2), vol_ma=100.0)
    assert snap is not None and snap.adx > 30.0
    assert snap.direction == "up"

    chop = StreamingRegime()
    snap = None
    for i in range(120):
        price = 100.0 + (1.0 if i % 2 else -1.0)
        snap = chop.update(_bar(i, price, price + 0.3, price - 0.3, price), vol_ma=100.0)
    assert snap is not None and snap.adx < 30.0


def test_low_liquidity_is_labelled_from_the_volume_ratio() -> None:
    regime = StreamingRegime()
    snap = None
    for i in range(60):
        snap = regime.update(_bar(i, 100, 100.2, 99.8, 100, v=10.0), vol_ma=100.0)
    assert snap is not None and snap.label == "low_liquidity"


def test_sessions_map_to_the_documented_utc_buckets() -> None:
    day = 1_700_000_000_000 - 1_700_000_000_000 % 86_400_000
    assert session_of(day + 1 * 3_600_000) == "asia_early"
    assert session_of(day + 5 * 3_600_000) == "asia_late"
    assert session_of(day + 9 * 3_600_000) == "europe"
    assert session_of(day + 14 * 3_600_000) == "us"
    assert session_of(day + 22 * 3_600_000) == "off_hours"


# --- expectancy engine ----------------------------------------------------


def test_expectancy_withholds_judgement_until_it_has_history() -> None:
    ev = ExpectancyEngine(min_samples=20)
    for _ in range(19):
        ev.observe("range|long", -1.0)
    # 19 straight losses is still not enough to reject: the threshold is the
    # guard against fitting a filter to a handful of trades
    assert ev.expectancy("range|long") is None
    assert ev.verdict("range|long") == "TRADE"
    ev.observe("range|long", -1.0)
    assert ev.verdict("range|long") == "REJECT"


def test_expectancy_only_ever_reflects_trades_already_observed() -> None:
    """The engine must never be able to score a trade it has not been told about."""
    ev = ExpectancyEngine(min_samples=5)
    for _ in range(5):
        ev.observe("range|long", 2.0)
    before = ev.expectancy("range|long")
    assert before == pytest.approx(2.0)
    ev.observe("range|long", -1.0)
    # adding a loss can only move the estimate afterwards, never retroactively
    assert ev.expectancy("range|long") < before


def test_expectancy_separates_buckets() -> None:
    ev = ExpectancyEngine(min_samples=5)
    for _ in range(5):
        ev.observe("trending|long", -1.0)
        ev.observe("range|short", 3.0)
    assert ev.verdict("trending|long") == "REJECT"
    assert ev.verdict("range|short") == "TRADE"


# --- gate composition -----------------------------------------------------


class _StubArm:
    """Always arms, so the gate is the only thing under test."""

    warmup_bars = 0
    last_confidence = 100
    last_reason = "structure_bounce long sr conf=100 confluence=2 htf=up"

    def __init__(self, side="long"):
        from vnedge.execution.trigger_engine import ArmState

        self.arm = ArmState(
            episode_id=1, box_high=101.0, box_low=99.0, compressed=True,
            atr=1.0, vol_ma=100.0, prev_close=100.0, side_hint=side,
        )

    def observe(self, ctx):
        return self.arm


def _ctx(bars, index, vol_ma=100.0):
    from vnedge.strategy.arm_sources import BarContext

    return BarContext(
        bars=bars, index=index, atr=1.0, vol_ma=vol_ma, vwap=100.0,
        prev_close=bars[index - 1][4] if index else bars[0][4],
    )


def test_gate_blocks_a_quiet_tape_as_low_liquidity() -> None:
    gate = ProductionGate(inner=_StubArm())
    bars = [_bar(i, 100, 100.2, 99.8, 100, v=10.0) for i in range(60)]
    out = [gate.observe(_ctx(bars, i)) for i in range(1, 60)]
    assert all(o is None for o in out)
    assert gate.blocked.get("regime:low_liquidity", 0) > 0


def test_gate_blocks_counter_trend_entries_in_a_trend() -> None:
    gate = ProductionGate(inner=_StubArm(side="short"))
    bars = [
        _bar(i, 100 + i * 0.5, 100 + i * 0.5 + 0.3, 100 + i * 0.5 - 0.1, 100 + i * 0.5 + 0.2)
        for i in range(150)
    ]
    for i in range(1, 150):
        gate.observe(_ctx(bars, i))
    assert gate.blocked.get("counter_trend", 0) > 0


def test_disabling_a_layer_removes_its_blocks() -> None:
    bars = [_bar(i, 100, 100.2, 99.8, 100, v=10.0) for i in range(60)]
    gate = ProductionGate(inner=_StubArm(), use_regime=False, min_confidence=0)
    for i in range(1, 60):
        gate.observe(_ctx(bars, i))
    assert "regime:low_liquidity" not in gate.blocked
    assert gate.passed > 0


def test_the_hour_gate_blocks_rather_than_discounts() -> None:
    """A confidence nudge still lets a marginal setup through in a dead hour.

    Median hourly range is 2.4-2.75x higher at 14:00 UTC than in the overnight
    trough, so a fixed round-trip cost consumes a far larger share of the move
    there. The gate has to be a hard block for that to matter.
    """
    from vnedge.strategy.arm_sources import BarContext
    from vnedge.strategy.production_filters import ProductionGate

    class _AlwaysArms:
        warmup_bars = 0
        last_confidence = 100
        last_reason = "structure_bounce long sr conf=100 confluence=2 htf=up"

        def observe(self, ctx):
            from vnedge.execution.trigger_engine import ArmState
            return ArmState(episode_id=1, box_high=101.0, box_low=99.0,
                            compressed=True, atr=1.0, vol_ma=100.0,
                            prev_close=100.0, side_hint="long")

    def _ctx_at(hour):
        ts = 1_787_000_000_000
        ts -= ts % 86_400_000
        ts += hour * 3_600_000
        bars = [(ts + i * 300_000, 100.0, 101.0, 99.0, 100.0, 200.0) for i in range(3)]
        return BarContext(bars=bars, index=2, atr=1.0, vol_ma=100.0,
                          vwap=100.0, prev_close=100.0)

    gate = ProductionGate(inner=_AlwaysArms(), allowed_hours=(12, 13, 14, 15),
                          use_regime=False, use_ev=False,
                          use_counter_trend_block=False,
                          use_confluence_required=False, use_fee_check=False,
                          use_session=False, min_confidence=0)
    assert gate.observe(_ctx_at(14)) is not None, "14:00 UTC must pass"
    assert gate.observe(_ctx_at(4)) is None, "04:00 UTC must be blocked"
    assert gate.blocked.get("session_hour") == 1

    ungated = ProductionGate(inner=_AlwaysArms(), use_regime=False, use_ev=False,
                             use_counter_trend_block=False,
                             use_confluence_required=False, use_fee_check=False,
                             use_session=False, min_confidence=0)
    assert ungated.observe(_ctx_at(4)) is not None, "no gate trades every hour"
