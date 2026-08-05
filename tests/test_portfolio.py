import numpy as np
import pandas as pd

from vnedge.backtest.portfolio import combine_portfolio, trades_to_daily_pnl


class _T:
    def __init__(self, exit_ts, net):
        self.exit_ts = pd.Timestamp(exit_ts, tz="UTC")
        self.net_pnl_usd = net


def _dates(n):
    return pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC")


def test_trades_to_daily_pnl_groups_by_exit_date():
    trades = [_T("2025-01-01T04:00Z", 5.0), _T("2025-01-01T20:00Z", -2.0),
              _T("2025-01-03T01:00Z", 3.0)]
    s = trades_to_daily_pnl(trades)
    assert s.loc[pd.Timestamp("2025-01-01", tz="UTC")] == 3.0
    assert s.loc[pd.Timestamp("2025-01-03", tz="UTC")] == 3.0
    assert len(s) == 2


def test_anticorrelated_legs_cancel_risk():
    d = _dates(60)
    a = pd.Series(np.tile([4.0, -3.0], 30), index=d)   # zig
    b = pd.Series(np.tile([-4.0, 3.0], 30), index=d)   # exact zag
    r = combine_portfolio({"a": a, "b": b}, starting_equity=1000.0, weighting="equal")
    # perfectly anti-correlated + equal weight -> portfolio daily pnl ~ 0
    assert abs(r.daily_pnl.abs().sum()) < 1e-9
    assert r.correlation.loc["a", "b"] < -0.99
    assert abs(r.max_dd_pct) < 1e-6  # no drawdown


def test_uncorrelated_legs_raise_sharpe_and_cut_drawdown():
    rng = np.random.default_rng(42)
    d = _dates(400)
    # two independent positive-drift noisy edges
    a = pd.Series(rng.normal(0.5, 6.0, len(d)), index=d)
    b = pd.Series(rng.normal(0.5, 6.0, len(d)), index=d)
    r = combine_portfolio({"a": a, "b": b}, starting_equity=1000.0, weighting="equal")
    leg_sharpes = [l.sharpe for l in r.legs]
    leg_dd = [abs(l.max_dd_pct) for l in r.legs]
    # diversification (guaranteed for uncorrelated legs): combined Sharpe beats
    # the AVERAGE leg, and drawdown beats the WORST leg.
    assert r.sharpe > sum(leg_sharpes) / len(leg_sharpes)
    assert abs(r.max_dd_pct) < max(leg_dd)


def test_inverse_vol_downweights_the_noisier_leg():
    d = _dates(200)
    calm = pd.Series(np.tile([1.0, -0.5], 100), index=d)
    wild = pd.Series(np.tile([10.0, -9.0], 100), index=d)
    r = combine_portfolio({"calm": calm, "wild": wild},
                          starting_equity=1000.0, weighting="inverse_vol")
    assert r.weights["calm"] > r.weights["wild"]
    assert abs(sum(r.weights.values()) - 1.0) < 1e-9


def test_single_leg_is_itself():
    d = _dates(30)
    a = pd.Series(np.linspace(-2, 2, 30), index=d)
    r = combine_portfolio({"a": a}, starting_equity=1000.0)
    assert r.weights["a"] == 1.0
    assert abs(r.net_usd - float(a.sum())) < 1e-9
