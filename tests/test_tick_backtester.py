"""Tick backtester — the HF economics validator."""

from datetime import UTC, datetime
from decimal import Decimal

from vnedge.backtest.tick_backtester import run_tick_backtest, slippage_sweep
from vnedge.risk.cost_gate import CostGate, CostProfile
from vnedge.strategy.mean_reversion_engine import ShortTermMeanReversionEngine
from vnedge.strategy.signal_engine import TickSnapshot


def _tick(mid, sec):
    m = Decimal(str(mid))
    half = Decimal("0.01")
    return TickSnapshot(
        symbol="BTCUSDT", ts=datetime(2026, 1, 1, 0, 0, sec, tzinfo=UTC),
        last_price=m, bid=m - half, ask=m + half,
        bid_size=Decimal("1"), ask_size=Decimal("1"),
    )


def test_ranging_series_produces_trades_and_coherent_report():
    # oscillating (ranging) series → MR fires on dips/peaks and reverts
    ticks = [_tick("100.40" if i % 2 else "99.60", i) for i in range(50)]
    gate = CostGate(CostProfile.SCALP, min_net_edge_bps=Decimal("2.0"))
    rep = run_tick_backtest(ticks, [ShortTermMeanReversionEngine(symbol="BTCUSDT")], gate)
    assert rep.signals_generated > 0
    assert rep.cost_gate_survived > 0
    assert rep.trades > 0
    assert 0.0 <= rep.survival_rate <= 1.0
    assert rep.trades_per_day > 0
    assert rep.wins + rep.losses == rep.trades
    assert rep.verdict in ("POSITIVE", "MARGINAL", "NEGATIVE")


def test_flat_series_generates_no_trades():
    ticks = [_tick("100.00", i) for i in range(50)]
    gate = CostGate(CostProfile.SCALP)
    rep = run_tick_backtest(ticks, [ShortTermMeanReversionEngine(symbol="BTCUSDT")], gate)
    assert rep.trades == 0 and rep.verdict == "NO_TRADES"


def test_slippage_sweep_is_monotonic_and_trades_invariant():
    # extra slippage only shifts realized net; the trades themselves never change
    ticks = [_tick("100.40" if i % 2 else "99.60", i) for i in range(50)]
    gate = CostGate(CostProfile.SCALP, min_net_edge_bps=Decimal("2.0"))
    sweep = slippage_sweep(ticks, [ShortTermMeanReversionEngine(symbol="BTCUSDT")], gate,
                           grid=[Decimal("0"), Decimal("2"), Decimal("4")])
    assert [r.extra_cost_bps for r in sweep.rows] == [0.0, 2.0, 4.0]
    assert sweep.rows[0].avg_net_bps >= sweep.rows[1].avg_net_bps >= sweep.rows[2].avg_net_bps
    assert sweep.rows[0].trades == sweep.rows[1].trades == sweep.rows[2].trades  # slippage ≠ new trades


def test_realized_net_can_be_negative_even_when_costgate_approved():
    # the validator's whole point: a TP capped below the round-trip cost realizes a
    # LOSS even though the CostGate approved on the (optimistic) edge estimate.
    ticks = [_tick("100.40" if i % 2 else "99.60", i) for i in range(50)]
    gate = CostGate(CostProfile.SCALP, min_net_edge_bps=Decimal("2.0"))
    rep = run_tick_backtest(ticks, [ShortTermMeanReversionEngine(symbol="BTCUSDT")], gate)
    # TP=12bps vs ~14bps taker round-trip → realized avg net is <= 0 despite approval
    assert rep.avg_net_bps <= 0
