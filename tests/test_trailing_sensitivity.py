"""Trailing-stop parameter sensitivity sweep."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from vnedge.backtest.trailing_sensitivity import default_grid, sweep_trailing
from vnedge.strategy.signal_engine import SignalIntent, TickSnapshot

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _tk(mid, sec):
    m = Decimal(str(mid))
    return TickSnapshot(symbol="B", ts=T0 + timedelta(seconds=sec), last_price=m,
                        bid=m - Decimal("0.01"), ask=m + Decimal("0.01"),
                        bid_size=Decimal("1"), ask_size=Decimal("1"))


def _sig():
    return SignalIntent(symbol="B", side="buy", stop_distance_bps=Decimal("40"),
                        take_profit_bps=Decimal("60"), urgency="taker",
                        edge_estimate_bps=Decimal("30"), expected_holding_seconds=900,
                        signal_id="s", ts=T0)


def test_sweep_ranks_and_tighter_trail_captures_more():
    ticks = [_tk(100.0, 0), _tk(100.5, 1), _tk(100.5, 2), _tk(100.3, 3), _tk(100.3, 500)]
    grid = default_grid(initial_stops=(20.0,), activations=(8.0,), distances=(10.0, 15.0),
                        time_caps=(900,))
    rep = sweep_trailing(ticks, [(0, _sig())], grid, fee_bps=5, slip_bps=2)
    assert rep.n_configs == 2 and rep.best_by_avg_net is not None
    assert 0.0 <= rep.pct_positive <= 100.0
    r10 = next(r for r in rep.results if "d10" in r.name)
    r15 = next(r for r in rep.results if "d15" in r.name)
    assert r10.avg_net_bps == 33.0 and r15.avg_net_bps == 28.0   # trail 10 keeps more of the +50


def test_default_grid_is_54():
    assert len(default_grid()) == 3 * 3 * 3 * 2
