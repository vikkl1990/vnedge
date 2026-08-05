"""Combine the two live earners into one risk-managed book + measure diversification."""
import sys
sys.path.insert(0, "src")
import pandas as pd
from vnedge.backtest.backtester import run_backtest, BacktestConfig
from vnedge.backtest.portfolio import trades_to_daily_pnl, combine_portfolio
from vnedge.data.parquet_store import ParquetStore
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion
from vnedge.strategy.crypto_trend_atr_margin import CryptoTrendAtrMargin

store = ParquetStore("data")
W = pd.Timestamp("2025-07-04", tz="UTC")

btc = store.read_candles("binanceusdm", "BTC/USDT:USDT", "1h"); btc = btc[btc["timestamp"] >= W].reset_index(drop=True)
btcf = store.read_funding("binanceusdm", "BTC/USDT:USDT")
fmr = FundingMeanReversion(btcf, extreme_pct=0.85, z_entry=1.5)
r1 = run_backtest(btc, btcf, fmr, BacktestConfig(), symbol="BTC/USDT:USDT", timeframe="1h")

doge = store.read_candles("binanceusdm", "DOGE/USDT:USDT", "1h"); doge = doge[doge["timestamp"] >= W].reset_index(drop=True)
dogef = store.read_funding("binanceusdm", "DOGE/USDT:USDT")
ct = CryptoTrendAtrMargin(fast_ema=30, slow_ema=60, atr_window=60, atr_margin_mult=0.30, stop_atr_mult=1.60, take_profit_r=None)
r2 = run_backtest(doge, dogef, ct, BacktestConfig(use_active_exit=True, trail_atr_mult=3.0), symbol="DOGE/USDT:USDT", timeframe="1h")

legs = {"funding_mr_BTC": trades_to_daily_pnl(r1.trades), "crypto_trend_DOGE": trades_to_daily_pnl(r2.trades)}
print(f"common window: {W.date()} -> 2026-08  (funding_mr {len(r1.trades)} trades, crypto_trend {len(r2.trades)} trades)")
print("="*84)
eq = combine_portfolio(legs, starting_equity=1000.0, weighting="equal")
iv = combine_portfolio(legs, starting_equity=1000.0, weighting="inverse_vol")

print("STANDALONE (each edge alone, $1000 book):")
for l in eq.legs:
    print(f"  {l.name:20s} net=${l.net_usd:+8.2f}  Sharpe={l.sharpe:+5.2f}  maxDD={l.max_dd_pct:+6.2f}%  days={l.days_traded}")
print(f"\nCORRELATION (daily PnL): {eq.correlation.iloc[0,1]:+.3f}")
print("\nPORTFOLIO (both, shared $1000 book):")
for name, r in (("equal-weight", eq), ("inverse-vol", iv)):
    w = " / ".join(f"{k.split('_')[0]}:{v:.2f}" for k, v in r.weights.items())
    print(f"  {name:13s} net=${r.net_usd:+8.2f}  Sharpe={r.sharpe:+5.2f}  maxDD={r.max_dd_pct:+6.2f}%   weights[{w}]")
print("="*84)
best_leg_sharpe = max(l.sharpe for l in eq.legs)
worst_leg_dd = min(l.max_dd_pct for l in eq.legs)  # most negative
print(f"DIVERSIFICATION: portfolio Sharpe {eq.sharpe:.2f} vs best-leg {best_leg_sharpe:.2f}; "
      f"portfolio maxDD {eq.max_dd_pct:.2f}% vs worst-leg {worst_leg_dd:.2f}%")
