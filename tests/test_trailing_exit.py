"""Trailing / wider-stop exit simulation."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from vnedge.backtest.trailing_exit import trailing_exit_backtest
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
                        edge_estimate_bps=Decimal("30"), expected_holding_seconds=100,
                        signal_id="s", ts=T0)


def test_trail_exits_winner_at_trailed_level():
    ticks = [_tk(100.0, 0), _tk(100.5, 1), _tk(100.5, 2), _tk(100.3, 3)]   # +50 peak, retrace to +30
    rep = trailing_exit_backtest(ticks, [(0, _sig())], init_stop_bps=40, trail_bps=10,
                                 arm_sec=0, time_cap_sec=100, cost_bps=14)
    assert rep.trades == 1 and rep.pct_trail == 100.0
    assert rep.avg_net_bps == 26.0                # gross = mfe 50 − trail 10 = 40; net = 40 − 14


def test_wide_stop_catches_loser():
    ticks = [_tk(100.0, 0), _tk(99.5, 1)]         # −50bps → −40 stop
    rep = trailing_exit_backtest(ticks, [(0, _sig())], init_stop_bps=40, trail_bps=10,
                                 arm_sec=0, time_cap_sec=100, cost_bps=14)
    assert rep.pct_stop == 100.0 and rep.avg_net_bps == -54.0   # −40 stop − 14 cost
