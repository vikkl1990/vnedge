"""PRE-REGISTERED 2nd-window judgment (edge_search_findings_20260803): funding_carry
+ session-gated funding_mr on the UNTOUCHED 2022-09 -> 2024-07 backfill window."""
import sys
sys.path.insert(0, "src")
import math as _m
import pandas as pd
from vnedge.backtest.backtester import BacktestConfig
from vnedge.backtest.walk_forward import PromotionGates, evaluate_promotion, walk_forward
from vnedge.data.parquet_store import ParquetStore
from vnedge.strategy.base_strategy import SignalIntent
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion

CUT = pd.Timestamp("2024-07-05", tz="UTC")
store = ParquetStore("data"); config = BacktestConfig(); gates = PromotionGates()


class FundingCarry(FundingMeanReversion):
    strategy_id = "funding_carry_v1"
    def __init__(self, funding, *, carry_pct=0.65, stop_atr_mult=3.0, **kw):
        super().__init__(funding, extreme_pct=carry_pct, z_entry=0.0, stop_atr_mult=stop_atr_mult, **kw)
        self.carry_pct = carry_pct
    def signal(self, df, index):
        if index < self.warmup_bars: return None
        row = df.iloc[index]
        if any(_m.isnan(float(row[c])) for c in ("atr","funding_pct","close")): return None
        close=float(row["close"]); stop=self.stop_atr_mult*float(row["atr"])
        if stop<=0: return None
        fp=float(row["funding_pct"])
        if "short" in self.allowed_sides and fp>=self.carry_pct and not row["regime_trend_up"]:
            return SignalIntent("short", stop_price=close+stop, reason=f"carry short fp={fp:.2f}")
        if "long" in self.allowed_sides and fp<=1.0-self.carry_pct and not row["regime_trend_down"]:
            return SignalIntent("long", stop_price=max(close-stop,1e-9), reason=f"carry long fp={fp:.2f}")
        return None
    def exit_signal(self, df, index, side, entry_price): return None


class FundingMRSession(FundingMeanReversion):
    strategy_id = "funding_mr_session_v1"
    def __init__(self, funding, *, s0=8, s1=20, **kw):
        super().__init__(funding, extreme_pct=0.85, z_entry=1.5, **kw); self.s0, self.s1 = s0, s1
    def signal(self, df, index):
        base=super().signal(df,index)
        if base is None: return None
        h=pd.Timestamp(df.iloc[index]["timestamp"]).hour
        return base if self.s0<=h<self.s1 else None


def run(name, sym, factory, funding):
    c = store.read_candles("binanceusdm", sym, "1h"); c = c[c["timestamp"] < CUT].reset_index(drop=True)
    f = funding[funding["timestamp"] < CUT].reset_index(drop=True)
    wf = walk_forward(c, f, factory, [{}], config, train_bars=2880, test_bars=720, step_bars=720, symbol=sym, timeframe="1h")
    v = evaluate_promotion(wf, gates); wins = wf.windows
    tr=sum(w.test_metrics.num_trades for w in wins); net=sum(w.test_metrics.net_profit_usd for w in wins)
    gw=gl=0.0
    for w in wins:
        m=w.test_metrics; nw=round(m.win_rate_pct/100*m.num_trades); gw+=m.avg_win_usd*nw; gl+=m.avg_loss_usd*(m.num_trades-nw)
    pf=gw/abs(gl) if gl else 0.0
    print(f"  {name:24s} {sym.split('/')[0]:4s} PASS={'YES' if v.passed else 'no':3s} splits={len(wins)} "
          f"trades={tr:4d} PF={pf:5.2f} net=${net:+8.2f} exp/tr=${(net/tr if tr else 0):+6.3f}")
    if not v.passed: print(f"       reject: {'; '.join(v.reject_reasons)[:80]}")

rng = store.read_candles("binanceusdm","BTC/USDT:USDT","1h"); rng = rng[rng["timestamp"]<CUT]
print(f"UNTOUCHED 2nd window: {rng['timestamp'].min()} -> {rng['timestamp'].max()} ({len(rng)} bars)")
print("-"*96)
for s in ("BTC/USDT:USDT","ETH/USDT:USDT"):
    f=store.read_funding("binanceusdm",s)
    run("funding_carry_v1", s, lambda **p: FundingCarry(f, **p), f)
fb=store.read_funding("binanceusdm","BTC/USDT:USDT")
run("funding_mr_session (BTC)", "BTC/USDT:USDT", lambda **p: FundingMRSession(fb, **p), fb)
