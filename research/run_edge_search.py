"""Edge search — grounded crypto-native candidates through the walk-forward gate.
Exploratory candidates live HERE (not in the production strategy tree) until
one passes. Every candidate is compared to the relevant incumbent."""
import sys
sys.path.insert(0, "src")
import pandas as pd
from vnedge.backtest.backtester import BacktestConfig
from vnedge.backtest.walk_forward import PromotionGates, evaluate_promotion, walk_forward
from vnedge.data.parquet_store import ParquetStore
from vnedge.strategy.base_strategy import SignalIntent
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion

store = ParquetStore("data")
config = BacktestConfig()
gates = PromotionGates()


class FundingMRSession(FundingMeanReversion):
    """Incumbent funding_mr, but entries only during peak-liquidity UTC sessions
    (Europe+US, 08:00-20:00). A priori structural gate, not fit to the sample."""
    strategy_id = "funding_mr_session_v1"

    def __init__(self, funding, *, session_start=8, session_end=20, **kw):
        super().__init__(funding, extreme_pct=0.85, z_entry=1.5, **kw)
        self.session_start, self.session_end = session_start, session_end

    def signal(self, df, index):
        base = super().signal(df, index)
        if base is None:
            return None
        hour = pd.Timestamp(df.iloc[index]["timestamp"]).hour
        return base if self.session_start <= hour < self.session_end else None


def summarize(name, sym, factory, funding):
    candles = store.read_candles("binanceusdm", sym, "1h")
    wf = walk_forward(candles, funding, factory, [{}], config,
                      train_bars=2880, test_bars=720, step_bars=720, symbol=sym, timeframe="1h")
    v = evaluate_promotion(wf, gates)
    wins = wf.windows
    tr = sum(w.test_metrics.num_trades for w in wins)
    net = sum(w.test_metrics.net_profit_usd for w in wins)
    gw = gl = 0.0
    for w in wins:
        m = w.test_metrics; nw = round(m.win_rate_pct/100*m.num_trades)
        gw += m.avg_win_usd*nw; gl += m.avg_loss_usd*(m.num_trades-nw)
    pf = gw/abs(gl) if gl else 0.0
    exp = net/tr if tr else 0.0
    print(f"  {name:26s} {sym.split('/')[0]:4s} PASS={'YES' if v.passed else 'no':3s} "
          f"trades={tr:4d} PF={pf:5.2f} net=${net:+8.2f} exp/tr=${exp:+6.3f}")
    return dict(tr=tr, pf=pf, exp=exp, net=net)

print("CANDIDATE 1: session-gated funding_mr (Europe+US 08-20 UTC) vs incumbent — BTC 1h")
print("-"*92)
fund = store.read_funding("binanceusdm", "BTC/USDT:USDT")
inc = summarize("incumbent 0.85/1.5", "BTC/USDT:USDT", lambda **p: FundingMeanReversion(fund, extreme_pct=0.85, z_entry=1.5, **p), fund)
ses = summarize("session-gated 08-20", "BTC/USDT:USDT", lambda **p: FundingMRSession(fund, **p), fund)
print("-"*92)
drop = 100*(1-ses["tr"]/max(inc["tr"],1))
better = ses["pf"]>inc["pf"] and ses["exp"]>inc["exp"] and drop<=50
print(f"  trades {inc['tr']}->{ses['tr']} ({drop:+.0f}%)  PF {inc['pf']:.2f}->{ses['pf']:.2f}  "
      f"exp ${inc['exp']:+.3f}->${ses['exp']:+.3f}  => {'ACCEPT' if better else 'REJECT'}")


class FundingCarry(FundingMeanReversion):
    """Perp-native CARRY (distinct mechanic from funding_mr's reversion fade):
    when funding is persistently one-sided, take the side that COLLECTS funding
    (short when funding positive, long when negative) and hold to accrue it —
    no price z-score, wider stop, time-based exit. Edge = the funding stream,
    not a snap-back. Reuses funding_mr's prepared columns."""
    strategy_id = "funding_carry_v1"

    def __init__(self, funding, *, carry_pct=0.65, stop_atr_mult=3.0, hold_bars=24, **kw):
        super().__init__(funding, extreme_pct=carry_pct, z_entry=0.0,
                         stop_atr_mult=stop_atr_mult, **kw)
        self.carry_pct = carry_pct
        self.hold_bars = hold_bars

    def signal(self, df, index):
        if index < self.warmup_bars:
            return None
        row = df.iloc[index]
        import math as _m
        if any(_m.isnan(float(row[c])) for c in ("atr", "funding_pct", "close")):
            return None
        close = float(row["close"]); stop = self.stop_atr_mult * float(row["atr"])
        if stop <= 0:
            return None
        fp = float(row["funding_pct"])
        # short collects funding when funding positive & not strongly up-trending
        if "short" in self.allowed_sides and fp >= self.carry_pct and not row["regime_trend_up"]:
            return SignalIntent("short", stop_price=close + stop,
                                reason=f"funding carry short: fp={fp:.2f}")
        if "long" in self.allowed_sides and fp <= 1.0 - self.carry_pct and not row["regime_trend_down"]:
            return SignalIntent("long", stop_price=max(close - stop, 1e-9),
                                reason=f"funding carry long: fp={fp:.2f}")
        return None

    def exit_signal(self, df, index, side, entry_price):
        # hold to collect funding; time-based exit is enforced by max_holding.
        return None

print()
print("CANDIDATE 2: funding-carry (collect funding) — BTC + ETH 1h")
print("-"*92)
for s in ("BTC/USDT:USDT", "ETH/USDT:USDT"):
    f = store.read_funding("binanceusdm", s)
    summarize("funding_carry_v1", s, lambda **p: FundingCarry(f, **p), f)
