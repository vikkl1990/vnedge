"""Is crypto_trend DOGE's forward loss regime (chop) or decay (death)?
Monthly PnL + monthly efficiency-ratio (trend regime) over the full arc."""
import sys
sys.path.insert(0, "src")
import pandas as pd, numpy as np
from vnedge.backtest.backtester import run_backtest, BacktestConfig
from vnedge.data.parquet_store import ParquetStore
from vnedge.strategy.crypto_trend_atr_margin import CryptoTrendAtrMargin
from vnedge.strategy.indicators import efficiency_ratio

store = ParquetStore("data")
doge = store.read_candles("binanceusdm", "DOGE/USDT:USDT", "1h")
dogef = store.read_funding("binanceusdm", "DOGE/USDT:USDT")
ct = CryptoTrendAtrMargin(fast_ema=30, slow_ema=60, atr_window=60, atr_margin_mult=0.30, stop_atr_mult=1.60, take_profit_r=None)
res = run_backtest(doge, dogef, ct, BacktestConfig(use_active_exit=True, trail_atr_mult=3.0), symbol="DOGE/USDT:USDT", timeframe="1h")

# monthly PnL
tr = pd.DataFrame([(pd.Timestamp(t.exit_ts).to_period("M"), float(t.net_pnl_usd)) for t in res.trades], columns=["m","pnl"])
mp = tr.groupby("m")["pnl"].agg(["sum","count"])
# monthly regime (avg 60-bar efficiency ratio = trendiness)
d = doge.copy(); d["er"] = efficiency_ratio(d["close"], 60); d["m"] = d["timestamp"].dt.to_period("M")
er = d.groupby("m")["er"].mean()

print("month     net$    trades   avgER(trend)   cum$")
print("-"*52)
cum=0.0
for m in mp.index:
    cum += mp.loc[m,"sum"]
    e = er.get(m, float("nan"))
    flag = "◀ ranging" if e<0.30 else ("trending" if e>0.42 else "")
    print(f"{str(m)}   {mp.loc[m,'sum']:+7.2f}  {int(mp.loc[m,'count']):4d}    {e:5.3f} {flag:9s}  {cum:+7.2f}")
print("-"*52)
# chop-vs-death test: are losing months lower-ER (ranging) than winning months?
mp2 = mp.join(er.rename("er"))
win = mp2[mp2["sum"]>0]; los = mp2[mp2["sum"]<=0]
print(f"WINNING months: n={len(win)}  avg ER={win['er'].mean():.3f}")
print(f"LOSING  months: n={len(los)}  avg ER={los['er'].mean():.3f}")
print(f"=> {'CHOP: losses cluster in low-ER ranging regimes (edge is regime-gated, not dead)' if los['er'].mean() < win['er'].mean()-0.02 else 'DEATH: loses regardless of regime (edge gone)'}")
