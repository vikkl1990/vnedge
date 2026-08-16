"""Profit ladder analysis."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from vnedge.backtest.profit_ladder import analyze_profit_ladder
from vnedge.strategy.signal_engine import SignalIntent, TickSnapshot

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _tk(mid, sec):
    m = Decimal(str(mid))
    return TickSnapshot(symbol="B", ts=T0 + timedelta(seconds=sec), last_price=m,
                        bid=m - Decimal("0.01"), ask=m + Decimal("0.01"),
                        bid_size=Decimal("1"), ask_size=Decimal("1"))


def _sig(side):
    return SignalIntent(symbol="B", side=side, stop_distance_bps=Decimal("12"),
                        take_profit_bps=Decimal("18"), urgency="taker",
                        edge_estimate_bps=Decimal("20"), expected_holding_seconds=45,
                        signal_id="s", ts=T0)


def test_ladder_rising_profit_grows_and_records_once_per_checkpoint():
    ticks = [_tk(100.0 * (1 + 0.004 * min(sec, 80) / 80), sec) for sec in range(120)]
    rep = analyze_profit_ladder(ticks, [(0, _sig("buy"))], checkpoints=[5, 10, 30])
    assert rep.pct_trades_ever_profitable == 100.0
    assert rep.median_time_to_first_profit_sec > 0
    cps = {c.sec: c for c in rep.checkpoints}
    assert cps[5].n_samples == 1 and cps[30].n_samples == 1        # recorded ONCE per signal
    assert cps[30].avg_unrealized_bps > cps[5].avg_unrealized_bps  # profit grows in a ramp
    assert cps[10].pct_currently_positive == 100.0


def test_ladder_falling_series_never_profitable():
    ticks = [_tk(100.0 * (1 - 0.004 * min(sec, 80) / 80), sec) for sec in range(120)]
    rep = analyze_profit_ladder(ticks, [(0, _sig("buy"))], checkpoints=[5, 30])
    assert rep.pct_trades_ever_profitable == 0.0
    assert all(c.pct_currently_positive == 0.0 for c in rep.checkpoints)
