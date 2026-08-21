"""Exit parity: what the research path books vs what the runtime path books.

Spec P0 2.2 asks for "same bars -> same exit reasons and R multiples across
backtest/paper/shadow". Today they are two engines with two vocabularies:

    research  (execution.exit_engine, used by scanner_session / bounce_lanes)
              stop | failed_breakout | no_progress | time_4h | tp_ladder | max_age
    runtime   (runtime.active_exit, used by paper_runner / shadow_outcomes)
              stop | breakeven_stop | tp{n}_partial | tp{n}_final | take_profit
              | max_holding

RESOLVED 2026-08-20: research adopted runtime's semantics rather than the
reverse -- the runtime engine is what actually trades, and a backtest exists
to predict it. no-progress is now opt-in and off by default, and the reason
vocabulary matches. What remains is the ladder UNIT difference, which is a
real conversion a caller must perform, not a defect.
"""

from __future__ import annotations

from vnedge.execution.exit_engine import ExitConfig, ExitEngine as ResearchExit
from vnedge.runtime.active_exit import ActiveExitState

ENTRY, STOP = 100.0, 99.0
RISK = ENTRY - STOP


def _research(**overrides) -> ResearchExit:
    engine = ResearchExit(config=ExitConfig(failed_breakout=False, **overrides))
    engine.open_from_fire(side="long", entry=ENTRY, stop=STOP, risk=RISK,
                          box_edge=ENTRY, entry_bar=0)
    return engine


def _runtime() -> ActiveExitState:
    return ActiveExitState(side="long", initial_stop_price=STOP, entry_price=ENTRY,
                           original_quantity=1.0)


def _drive_research(engine, bars):
    for i, (high, low, close) in enumerate(bars, start=1):
        decision = engine.on_bar(high=high, low=low, close=close, atr=0.5, bar_index=i)
        if decision is not None:
            return decision.reason, decision.price
    return None, None


def _drive_runtime(state, bars, *, max_holding=None):
    for i, (high, low, close) in enumerate(bars, start=1):
        decision = state.resolve_bar(
            high=high, low=low, close=close, position_quantity=1.0,
            max_holding_hit=max_holding is not None and i >= max_holding,
        )
        if decision is not None:
            return decision.reason, decision.exit_price
    return None, None


def test_a_hard_stop_agrees_in_both_engines() -> None:
    """The one case that must never diverge: price traded through the stop."""
    bars = [(100.5, 98.5, 98.7)]
    assert _drive_research(_research(), bars) == ("stop", STOP)
    assert _drive_runtime(_runtime(), bars) == ("stop", STOP)


def test_a_drifting_trade_is_now_HELD_by_both_engines() -> None:
    """The divergence that most changed a book, now closed.

    no-progress closed 29% of exits at -11.2 bps each on the structure-bounce
    arm while runtime.active_exit had no such rule, so a shadow lane running
    the same signals booked a materially different set of trades. Research now
    defaults to runtime's semantics: a backtest exists to predict the engine
    that actually trades.
    """
    drift = [(100.2, 99.8, 100.0)] * 8
    assert _drive_research(_research(), drift) == (None, None)
    assert _drive_runtime(_runtime(), drift) == (None, None)


def test_no_progress_is_opt_in_and_flagged_as_research_only() -> None:
    """Still available for a research question -- but never by default."""
    drift = [(100.2, 99.8, 100.0)] * 8
    reason, _ = _drive_research(_research(no_progress_bars=4), drift)
    assert reason == "no_progress"
    assert ExitConfig().no_progress_bars is None


def test_the_time_cap_now_reports_the_same_reason_in_both() -> None:
    """Exit histograms are only mergeable if the same event has one name."""
    quiet = [(100.2, 99.8, 100.0)] * 50
    research_reason, _ = _drive_research(_research(absolute_max_bars=48), quiet)
    runtime_reason, _ = _drive_runtime(_runtime(), quiet, max_holding=48)

    assert research_reason == "max_holding"
    assert runtime_reason == "max_holding"


def test_a_stop_after_breakeven_armed_is_named_breakeven_stop() -> None:
    """Both engines now distinguish a ratcheted stop from the original one."""
    bars = [(101.5, 100.0, 101.2), (101.3, 98.5, 98.6)]
    research_reason, _ = _drive_research(_research(), bars)
    assert research_reason == "breakeven_stop"

    state = _runtime()
    state.resolve_bar(high=101.5, low=100.0, close=101.2, position_quantity=1.0)
    runtime_reason, _ = _drive_runtime(state, [(101.3, 98.5, 98.6)])
    assert runtime_reason in {"stop", "breakeven_stop"}


def test_ladders_are_expressed_in_different_units() -> None:
    """Research ladders in R multiples, runtime in absolute prices.

    The same intent must be converted, not copied, when a research config is
    handed to a runtime lane -- a silent copy would place the rungs elsewhere.
    """
    research = _research(tp_ladder=((1.5, 0.4), (2.5, 0.6)))
    reason, price = _drive_research(research, [(103.0, 100.0, 102.9)])
    assert reason == "tp_ladder"
    # 1.5R and 2.5R above a 1.00 risk => 101.50 and 102.50 in price terms
    state = ActiveExitState(side="long", initial_stop_price=STOP, entry_price=ENTRY,
                            original_quantity=1.0,
                            take_profit_levels=(101.5, 102.5))
    runtime_reason, runtime_price = _drive_runtime(state, [(103.0, 100.0, 102.9)])
    assert runtime_reason.startswith("tp1")
    assert runtime_price == 101.5
