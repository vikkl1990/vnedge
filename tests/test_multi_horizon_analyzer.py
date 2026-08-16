"""Multi-horizon holding & excursion analysis."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from vnedge.backtest.multi_horizon_analyzer import analyze_horizons
from vnedge.strategy.signal_engine import SignalIntent, TickSnapshot

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _tk(mid, sec):
    m = Decimal(str(mid))
    half = Decimal("0.01")
    return TickSnapshot(symbol="B", ts=T0 + timedelta(seconds=sec), last_price=m,
                        bid=m - half, ask=m + half, bid_size=Decimal("1"), ask_size=Decimal("1"))


def _sig(side):
    return SignalIntent(symbol="B", side=side, stop_distance_bps=Decimal("12"),
                        take_profit_bps=Decimal("18"), urgency="taker",
                        edge_estimate_bps=Decimal("20"), expected_holding_seconds=45,
                        signal_id="s", ts=T0)


def test_rising_series_hits_tp_within_horizon():
    ticks = [_tk(100.0 * (1 + 0.004 * min(sec, 80) / 80), sec) for sec in range(120)]
    rep = analyze_horizons(ticks, [(0, _sig("buy"))], horizons=[60, 300], cost_bps=14.0)
    r60 = next(r for r in rep.summaries if r.horizon_sec == 60)
    assert r60.pct_hit_tp == 100.0 and r60.avg_mfe_bps > 0
    assert r60.avg_final_net_bps == 4.0        # TP 18 − cost 14
    assert r60.median_time_to_mfe_sec > 0


def test_falling_series_hits_sl_within_horizon():
    ticks = [_tk(100.0 * (1 - 0.004 * min(sec, 80) / 80), sec) for sec in range(120)]
    rep = analyze_horizons(ticks, [(0, _sig("buy"))], horizons=[60], cost_bps=14.0)
    r = rep.summaries[0]
    assert r.pct_hit_sl == 100.0 and r.avg_mae_bps < 0
    assert r.avg_final_net_bps == -26.0        # SL −12 − cost 14


def test_override_stop_tp():
    ticks = [_tk(100.0 * (1 + 0.004 * min(sec, 80) / 80), sec) for sec in range(120)]
    # override TP to 12 (tighter) → still TP, net = 12 − 14 = −2
    rep = analyze_horizons(ticks, [(0, _sig("buy"))], horizons=[60], cost_bps=14.0,
                           stop_bps_override=14.0, tp_bps_override=12.0)
    assert rep.summaries[0].avg_final_net_bps == -2.0
