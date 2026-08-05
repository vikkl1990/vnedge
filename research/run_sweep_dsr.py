"""Bounded, honesty-gated sweep: 3 mechanic families x param grids x timeframes on
BTC, each judged via walk-forward OOS, then the WHOLE set penalised by the
Deflated Sharpe Ratio (n_trials = every config tried). DSR<~0.95 => the best
backtest is not convincingly real once the search breadth is accounted for."""
import sys, math, itertools
sys.path.insert(0, "src")
import numpy as np, pandas as pd
from vnedge.backtest.backtester import BacktestConfig
from vnedge.backtest.walk_forward import walk_forward
from vnedge.data.parquet_store import ParquetStore
from vnedge.ml.validation import deflated_sharpe_ratio
from vnedge.strategy.base_strategy import SignalIntent
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion
from vnedge.strategy.crypto_trend_atr_margin import CryptoTrendAtrMargin

store = ParquetStore("data"); cfg = BacktestConfig()
SYM = "BTC/USDT:USDT"
TFS = ["5m", "15m", "1h", "4h"]


class FundingCarry(FundingMeanReversion):
    strategy_id = "funding_carry"
    def __init__(self, funding, *, carry_pct=0.65, stop_atr_mult=3.0, **kw):
        super().__init__(funding, extreme_pct=carry_pct, z_entry=0.0, stop_atr_mult=stop_atr_mult, **kw)
        self.carry_pct = carry_pct
    def signal(self, df, i):
        if i < self.warmup_bars: return None
        r = df.iloc[i]
        if any(math.isnan(float(r[c])) for c in ("atr","funding_pct","close")): return None
        cl=float(r["close"]); st=self.stop_atr_mult*float(r["atr"])
        if st<=0: return None
        fp=float(r["funding_pct"])
        if fp>=self.carry_pct and not r["regime_trend_up"]: return SignalIntent("short", stop_price=cl+st)
        if fp<=1-self.carry_pct and not r["regime_trend_down"]: return SignalIntent("long", stop_price=max(cl-st,1e-9))
        return None
    def exit_signal(self,*a,**k): return None

# (family, id, factory-builder over params, param-grid)
FAMILIES = []
for cp, sm in itertools.product((0.60,0.65,0.70),(2.5,3.0)):
    FAMILIES.append(("carry", f"carry cp{cp} s{sm}", lambda f,cp=cp,sm=sm: (lambda **p: FundingCarry(f, carry_pct=cp, stop_atr_mult=sm, **p))))
for ep, ze in itertools.product((0.80,0.85,0.90),(1.5,2.0)):
    FAMILIES.append(("fundmr", f"fundmr e{ep} z{ze}", lambda f,ep=ep,ze=ze: (lambda **p: FundingMeanReversion(f, extreme_pct=ep, z_entry=ze, **p))))
for (fa,sl), am in itertools.product(((20,50),(30,60),(50,100)),(0.3,0.5)):
    FAMILIES.append(("trend", f"trend {fa}/{sl} m{am}", lambda f,fa=fa,sl=sl,am=am: (lambda **p: CryptoTrendAtrMargin(fast_ema=fa, slow_ema=sl, atr_margin_mult=am, **p))))

rows=[]
for tf in TFS:
    c = store.read_candles("binanceusdm", SYM, tf)
    c = c[c["timestamp"] >= pd.Timestamp("2025-07-04", tz="UTC")].reset_index(drop=True)
    f = store.read_funding("binanceusdm", SYM)
    n=len(c); test_bars=max(300, n//12); train_bars=3*test_bars
    if train_bars+test_bars > n:
        print(f"  {tf}: only {n} bars — skip"); continue
    for fam, name, build in FAMILIES:
        fac = build(f)
        try:
            wf = walk_forward(c, f, fac, [{}], cfg, train_bars=train_bars, test_bars=test_bars, step_bars=test_bars, symbol=SYM, timeframe=tf)
        except Exception as e:
            continue
        trades=[t for w in wf.windows for t in w.test_trades]
        nets=[float(t.net_pnl_usd) for t in trades]
        if len(nets)<10:
            rows.append(dict(tf=tf,fam=fam,name=name,n=len(nets),net=sum(nets),sharpe=float("nan"),nets=nets)); continue
        a=np.array(nets); sh=a.mean()/a.std(ddof=1) if a.std(ddof=1)>0 else 0.0
        rows.append(dict(tf=tf,fam=fam,name=name,n=len(nets),net=float(a.sum()),sharpe=float(sh),nets=nets))

valid=[r for r in rows if r["n"]>=10 and not math.isnan(r["sharpe"])]
print(f"SWEEP: {len(rows)} configs run, {len(valid)} with >=10 OOS trades. BTC.")
print("-"*94)
print("  TOP 8 by raw per-trade Sharpe (the tempting 'winners'):")
for r in sorted(valid, key=lambda x:-x["sharpe"])[:8]:
    print(f"    {r['tf']:3s} {r['name']:22s} n={r['n']:4d} net=${r['net']:+8.2f} sharpe={r['sharpe']:+.3f}")
print("-"*94)
if len(valid)>=2:
    best=max(valid, key=lambda x:x["sharpe"])
    trial_sharpes=[r["sharpe"] for r in valid]
    dsr=deflated_sharpe_ratio(best["nets"], n_trials=len(valid), trial_sharpes=trial_sharpes)
    print(f"  BEST config: {best['tf']} {best['name']}  raw per-trade sharpe={best['sharpe']:.3f}")
    print(f"  DEFLATED SHARPE (penalised for {len(valid)} trials) = {dsr:.3f}")
    print(f"  => {'SURVIVES the multiple-comparison penalty (DSR>=0.95)' if dsr>=0.95 else 'FAILS — the best backtest is not convincingly real (likely noise)'}")
